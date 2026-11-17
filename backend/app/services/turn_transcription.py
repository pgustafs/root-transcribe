"""Transcribing a recording one speaker turn at a time.

Used when the speech-to-text server gives no word timestamps: each diarized
turn's audio is cut out and transcribed on its own, so who said what never
depends on the server's timing.

Two things go wrong with naive cuts, and both lose speech:

* A cut exactly at the turn's edges clips the first and last sound, and a
  speech model handed audio that starts mid-sound tends to miss the word. Each
  clip therefore gets a small margin of real audio, never past the midpoint to
  the neighbouring turn (so a neighbour's words stay out), and a little silence
  at both ends.
* A very short turn ("Hej!", "Hur mår du?") is too little audio for the model
  and comes back empty. Such a turn is transcribed again with a few seconds of
  context on both sides, and the neighbours' words, which are already known,
  are taken out of the result. What remains is the short turn.

Before any of that, turns by the same speaker with almost no pause between
them are joined: speaker detection often ends a turn in the middle of a
sentence ("... mellan Stockholm" / "och Sälen med bil"), and the two halves
transcribe worse apart than together.

Last, when the whole recording was also transcribed in one piece (plain text,
no timing), that text is the reference for what was said: words it has that no
turn has were lost in a turn's clip, typically a short second sentence after a
pause ("... lite trött." / "Hur mår du?"). They are put back where they
belong in the order of the talk, in the turn that has the time for them.

Pure logic: no audio, no network.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher

from app.services.transcript_align import SpeakerTurn

#: Real audio added before and after a turn's clip (seconds).
MARGIN_SECONDS = 0.2
#: Silence added at both ends of every clip.
SILENCE_SECONDS = 0.3
#: Context on each side when a turn that came back empty is tried again.
CONTEXT_SECONDS = 2.5
#: Only short turns are tried again; a long empty turn is not speech.
RESCUE_MAX_SECONDS = 8.0
#: A turn shorter than this cannot carry a word.
MIN_TURN_SECONDS = 0.2
#: Nobody says more words a second than this: a turn already this full cannot
#: be where missing words were spoken.
MAX_WORDS_PER_SECOND = 5.0
#: The whole-file text and the turns must broadly be the same talk before the
#: one is used to complete the other.
MIN_AGREEMENT = 0.6
#: Same-speaker turns with a shorter pause than this are one stretch of speech.
JOIN_GAP_SECONDS = 0.5
#: A joined turn stays within one request to the model (its 30 s window), so
#: joining never creates a cut in the middle of a word.
JOIN_MAX_SECONDS = 30.0
#: The longest piece of audio sent at once for the whole-file text when the
#: recording is too large for one request (the model's own window).
WINDOW_SECONDS = 30.0
#: A turn shorter than this is a piece of speech rather than a turn...
PIECE_SECONDS = 1.5
#: ...and belongs with its speaker's speech that is at most this far away.
PIECE_GAP_SECONDS = 2.5
#: A speaker change found a second early or late puts a few words on the
#: wrong side. More words than this are not such a slip: the sentence really
#: passed from one person to the other.
MAX_MOVED_WORDS = 5


def join_close_turns(
    turns: Sequence[SpeakerTurn],
    *,
    max_gap: float = JOIN_GAP_SECONDS,
    max_length: float = JOIN_MAX_SECONDS,
) -> list[SpeakerTurn]:
    """Join consecutive turns of one speaker that are less than ``max_gap``
    apart, as long as the result is at most ``max_length`` long. Turns with
    another speaker between them are never joined."""
    joined: list[SpeakerTurn] = []
    for turn in sorted(turns, key=lambda t: (t.start, t.end)):
        last = joined[-1] if joined else None
        if (
            last is not None
            and last.speaker == turn.speaker
            and turn.start - last.end < max_gap
            and max(last.end, turn.end) - last.start <= max_length
        ):
            joined[-1] = SpeakerTurn(last.speaker, last.start, max(last.end, turn.end))
        else:
            joined.append(turn)
    return joined


def clip_bounds(
    turns: Sequence[SpeakerTurn],
    index: int,
    duration: float,
    *,
    margin: float = MARGIN_SECONDS,
) -> tuple[float, float]:
    """Where to cut the audio for ``turns[index]``: the turn plus a margin on
    each side, at most half the pause to the neighbouring turn and never
    outside the recording."""
    turn = turns[index]
    lead = margin
    if index > 0:
        lead = min(lead, max(0.0, (turn.start - turns[index - 1].end) / 2))
    tail = margin
    if index + 1 < len(turns):
        tail = min(tail, max(0.0, (turns[index + 1].start - turn.end) / 2))
    start = max(0.0, turn.start - lead)
    # A turn may end past the header's duration by a rounding step: never pull
    # its own end in.
    end = max(turn.end, min(duration, turn.end + tail))
    return start, end


def window_bounds(
    turns: Sequence[SpeakerTurn],
    duration: float,
    *,
    max_seconds: float = WINDOW_SECONDS,
    margin: float = MARGIN_SECONDS,
) -> list[tuple[float, float]]:
    """Cut the talk into pieces of at most ``max_seconds`` for the whole-file
    text of a recording too large to send in one piece.

    A piece ends only in a pause between turns (half-way through it, at most
    ``margin`` after the speech), so no word is cut in two. Speech that runs
    on for longer than ``max_seconds`` without any pause is cut every
    ``max_seconds``, like the speech server would. Silence longer than the
    margins is left out.
    """
    # Stretches of speech: overlapping or touching turns are one stretch.
    spans: list[list[float]] = []
    for turn in sorted(turns, key=lambda t: (t.start, t.end)):
        if spans and turn.start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], turn.end)
        else:
            spans.append([turn.start, turn.end])
    windows: list[tuple[float, float]] = []
    for index, (start, end) in enumerate(spans):
        lead = margin if index == 0 else min(margin, (start - spans[index - 1][1]) / 2)
        tail = (
            margin
            if index + 1 == len(spans)
            else min(margin, (spans[index + 1][0] - end) / 2)
        )
        start, end = max(0.0, start - lead), max(end, min(duration, end + tail))
        if windows and end - windows[-1][0] <= max_seconds:
            windows[-1] = (windows[-1][0], end)  # the pause in between goes too
            continue
        while end - start > max_seconds:
            windows.append((start, start + max_seconds))
            start += max_seconds
        windows.append((start, end))
    return windows


def context_bounds(
    turn: SpeakerTurn, duration: float, *, context: float = CONTEXT_SECONDS
) -> tuple[float, float]:
    """The wider cut used to try an empty turn again."""
    return max(0.0, turn.start - context), max(
        turn.end, min(duration, turn.end + context)
    )


def context_texts(
    turns: Sequence[SpeakerTurn],
    texts: Sequence[str],
    index: int,
    bounds: tuple[float, float],
) -> tuple[str, str]:
    """What is already known to be said around ``turns[index]``, for
    :func:`rescue_text`: the texts of every turn that reaches into the
    listening window ``bounds``, before and after. A few seconds of context
    often hold two or three turns; taking out only the nearest one leaves the
    others' words to be passed off as the empty turn's ("Jobba lite." twice).
    With no turn in the window on a side, the nearest heard turn is used.
    """
    start, end = bounds
    before = [texts[i] for i in range(index) if texts[i] and turns[i].end > start] or [
        next((t for t in reversed(texts[:index]) if t), "")
    ]
    after = [
        texts[i]
        for i in range(index + 1, len(turns))
        if texts[i] and turns[i].start < end
    ] or [next((t for t in texts[index + 1 :] if t), "")]
    return " ".join(before), " ".join(after)


#: Punctuation closing a word ("vi." -> "."); always matches, maybe empty.
_TAIL = re.compile(r"[^\w]*$", flags=re.UNICODE)
#: A word's first letter.
_LETTER = re.compile(r"[^\W\d_]", flags=re.UNICODE)


def _key(word: str) -> str:
    return re.sub(r"[^\w]+", "", word, flags=re.UNICODE).casefold()


def _words(text: str) -> list[str]:
    return [word for word in text.split() if _key(word)]


#: A dash standing alone. Speech models write one where the speaker changes
#: inside a clip ("- Ja. - Nej."); with one speaker a line it means nothing.
_DASH = re.compile(r"^[-\u2010-\u2015]+$")


def _tokens(text: str) -> list[str]:
    """The words of ``text`` for rebuilding it: a symbol standing alone ("&")
    is no word of its own but stays with the word before it (the word after
    it at the start), so "Bok & Demo" is not rebuilt as "Bok Demo". A dash
    standing alone is left out: it marks a change of speaker inside a clip,
    and kept, it would end up behind the last word of the line before
    ("använda? -") and hide that the sentence ended there."""
    tokens: list[str] = []
    lead = ""
    for part in text.split():
        if _DASH.match(part):
            continue
        if _key(part):
            tokens.append(f"{lead}{part}")
            lead = ""
        elif tokens:
            tokens[-1] = f"{tokens[-1]} {part}"
        else:
            lead = f"{lead}{part} "
    return tokens


def piece_runs(
    turns: Sequence[SpeakerTurn],
    texts: Sequence[str],
    *,
    piece_seconds: float = PIECE_SECONDS,
    max_gap: float = PIECE_GAP_SECONDS,
    max_length: float = JOIN_MAX_SECONDS,
) -> list[list[int]]:
    """Runs of one speaker's turns that are one stretch of speech cut into
    pieces: consecutive turns (nobody else in between), each at most
    ``max_gap`` after the one before, with at least one short piece (under
    ``piece_seconds``) that was heard ("" turns are left alone), and at most
    ``max_length`` long in all. Speaker detection cuts "till" / "företag." /
    "och myndigheter." out of one sentence; a fraction of a second of audio
    on its own is heard as one word, a wrong word or nothing.

    ``turns`` must be in order of time. Returns the turn indexes of each run.
    """
    runs: list[list[int]] = []
    current: list[int] = []

    def close() -> None:
        if len(current) > 1 and any(
            turns[i].end - turns[i].start < piece_seconds for i in current
        ):
            runs.append(list(current))
        current.clear()

    for index, (turn, text) in enumerate(zip(turns, texts, strict=True)):
        if not text.strip():
            close()  # nothing heard: no words to merge, and it may be a cough
            continue
        if current:
            last = turns[current[-1]]
            if (
                index == current[-1] + 1
                and last.speaker == turn.speaker
                and turn.start - last.end <= max_gap
                and turn.end - turns[current[0]].start <= max_length
            ):
                current.append(index)
                continue
            close()
        current.append(index)
    close()
    return runs


def pick_run_text(joined_text: str, piece_texts: Sequence[str]) -> str:
    """The text of a run of pieces: what the model heard in the whole run at
    once, unless that has fewer words than the pieces heard one by one (it
    then dropped some; a piece across a pause is easily missed, or a
    neighbour's overlapping words heard instead). Then the pieces' own words,
    joined. Words are never lost by merging."""
    pieces = " ".join(text.strip() for text in piece_texts if text.strip())
    if len(_words(joined_text)) >= len(_words(pieces)):
        return joined_text.strip()
    return pieces


def rescue_text(
    context_text: str,
    before_text: str,
    after_text: str,
    *,
    turn_seconds: float,
) -> str:
    """The words in ``context_text`` that belong to neither neighbour.

    ``context_text`` is what the model heard in a few seconds around an empty
    turn: the end of the turn before, the turn itself, the start of the turn
    after. The neighbours' texts are known, so their words are matched away
    (in order, tolerant of small differences) and the run of words left
    between them is the turn. Returns "" when nothing is left, or when so much
    is left that the matching clearly failed.
    """
    heard = _words(context_text)
    if not heard:
        return ""
    known = _words(before_text) + _words(after_text)
    matcher = SequenceMatcher(
        None, [_key(w) for w in heard], [_key(w) for w in known], autojunk=False
    )
    matched = [False] * len(heard)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            matched[block.a + offset] = True
    # The turn is one stretch of speech: take the longest run of unmatched
    # words (a stray unmatched word elsewhere is a transcription difference).
    best: tuple[int, int] = (0, 0)
    start: int | None = None
    for position, is_matched in enumerate([*matched, True]):
        if not is_matched and start is None:
            start = position
        elif is_matched and start is not None:
            if position - start > best[1] - best[0]:
                best = (start, position)
            start = None
    run = heard[best[0] : best[1]]
    # People say at most a handful of words a second: more than that left over
    # means the neighbours were not recognised, and this is their text.
    if not run or len(run) > max(4, int(turn_seconds * 6) + 2):
        return ""
    return " ".join(run)


def fill_from_full_text(
    full_text: str,
    turns: Sequence[SpeakerTurn],
    texts: Sequence[str],
) -> tuple[list[str], int]:
    """Put back words that the whole-file transcript has and no turn has.

    ``texts[i]`` is what was heard in ``turns[i]`` ("" when nothing). The two
    transcripts are lined up word by word. A run of words that exists only in
    the whole-file text was said at that point of the talk:

    * inside a turn's text: it goes back into that turn, in place;
    * between two turns: it goes to an empty turn lying between them if there
      is one, else to the neighbour that has the time for it (speech takes
      time: the turn that would stay slowest, and under the limit of
      ``MAX_WORDS_PER_SECOND``).

    Words that differ between the two transcripts are left alone: only pure
    additions are taken. Nothing is done when the transcripts do not broadly
    agree. Returns the completed texts and how many runs were put back.
    """
    words: list[list[str]] = [_tokens(text) for text in texts]
    flat: list[tuple[str, int]] = [
        (_key(word), index) for index, turn in enumerate(words) for word in turn
    ]
    full = _tokens(full_text)
    if not flat or not full:
        return list(texts), 0
    matcher = SequenceMatcher(
        None, [key for key, _ in flat], [_key(w) for w in full], autojunk=False
    )
    if matcher.ratio() < MIN_AGREEMENT:
        return list(texts), 0

    def seconds(index: int) -> float:
        return max(0.0, turns[index].end - turns[index].start)

    def fits(index: int, extra: int) -> float | None:
        """Words a second with ``extra`` more words, or None when too many."""
        length = seconds(index)
        if length < MIN_TURN_SECONDS:
            return None
        rate = (len(words[index]) + extra) / length
        return rate if rate <= MAX_WORDS_PER_SECOND else None

    def gap_for(i1: int, extra: int) -> int | None:
        """An empty turn lying between the turns around word position ``i1``."""
        before = flat[i1 - 1][1] if i1 > 0 else -1
        after = flat[i1][1] if i1 < len(flat) else len(turns)
        for index in range(before + 1, after):
            if not words[index] and fits(index, extra) is not None:
                return index
        return None

    def adopt(flat_index: int, full_index: int, *, before: bool) -> None:
        """Fit the word next to an insertion to the sentence as it now reads.

        It is the same word in both transcripts, and the whole-file text has
        the sentence with the inserted words in it. The word before them
        takes its closing punctuation ("det gör vi." + "ju." is "det gör vi
        ju.", not "det gör vi. ju."); the word after them takes its capital
        or small first letter ("Och" + "Vi pratar" is "Och vi pratar")."""
        if not (0 <= flat_index < len(flat) and 0 <= full_index < len(full)):
            return
        key, owner = flat[flat_index]
        offset = sum(1 for _word, other in flat[:flat_index] if other == owner)
        theirs = full[full_index]
        if offset >= len(words[owner]) or _key(theirs) != key:
            return
        mine = words[owner][offset]
        if _key(mine) != key:
            return
        if before:
            words[owner][offset] = _TAIL.sub("", mine) + _TAIL.search(theirs).group()
            return
        at, theirs_at = _LETTER.search(mine), _LETTER.search(theirs)
        if at and theirs_at:
            letter = mine[at.start()]
            letter = letter.upper() if theirs_at.group().isupper() else letter.lower()
            words[owner][offset] = mine[: at.start()] + letter + mine[at.end() :]

    added = 0
    # Back to front, so earlier positions stay valid while words are inserted.
    for tag, i1, _i2, j1, j2 in reversed(matcher.get_opcodes()):
        if tag != "insert":
            continue
        run = full[j1:j2]
        # "Hej! Hej!" against one "Hej!": the lining-up cannot know which of
        # the two is missing. The same words fit one step further on as long
        # as the next word equals the run's first; look there for an empty
        # turn before settling for a neighbour.
        target = gap_for(i1, len(run))
        shifted, at = run, i1
        while (
            target is None
            and at < len(flat)
            and at - i1 < 20
            and flat[at][0] == _key(shifted[0])
        ):
            shifted, at = [*shifted[1:], shifted[0]], at + 1
            target = gap_for(at, len(run))
        if target is not None:
            words[target] = list(shifted if at != i1 else run)
            added += 1
            continue
        before = flat[i1 - 1][1] if i1 > 0 else None
        after = flat[i1][1] if i1 < len(flat) else None
        if before is not None and before == after:
            # In the middle of a turn's own text.
            offset = sum(1 for _word, owner in flat[:i1] if owner == before)
            if fits(before, len(run)) is not None:
                adopt(i1 - 1, j1 - 1, before=True)
                adopt(i1, j2, before=False)
                words[before][offset:offset] = run
                added += 1
            continue
        # Between two turns: the neighbour that has the time for the words.
        options = [
            (rate, index, place)
            for index, place in ((before, "end"), (after, "start"))
            if index is not None and (rate := fits(index, len(run))) is not None
        ]
        if not options:
            continue
        _rate, target, place = min(options)
        if place == "start":
            adopt(i1, j2, before=False)
            words[target][0:0] = run
        else:
            adopt(i1 - 1, j1 - 1, before=True)
            words[target].extend(run)
        added += 1
    return [" ".join(turn) for turn in words], added


#: A word that ends a sentence ("använda?", "delade.", 'sa han."').
_SENTENCE_END = re.compile(r"[.?!\u2026][\"'\u201d\u2019)\]]*$")


def _set_first_letter(word: str, *, upper: bool) -> str:
    at = _LETTER.search(word)
    if at is None:
        return word
    letter = word[at.start()]
    return (
        word[: at.start()]
        + (letter.upper() if upper else letter.lower())
        + (word[at.end() :])
    )


def realign_sentences(
    full_text: str,
    turns: Sequence[SpeakerTurn],
    texts: Sequence[str],
    *,
    max_moved: int = MAX_MOVED_WORDS,
) -> tuple[list[str], int]:
    """Give a sentence that straddles a speaker change back to one speaker.

    Speaker detection finds a change of speaker a second early or late when
    there is no pause between the two ("Har du skrivit någon text" / "som vi
    kan använda? Japp, ..."): the words on the wrong side of the change then
    stand under the other person. The whole-file text has the sentence in one
    piece, so where two turns of different speakers share a sentence, the
    shorter part moves over to the longer one.

    Careful by design, because a wrong move puts words under the wrong person
    just as the slip did:

    * the first turn must break off: its own text does not end a sentence
      there. When it does ("... ifall de betalar." / "till företag och
      myndigheter."), the whole-file text running the two together is not
      enough to go on;
    * only words at the change itself move, at most ``max_moved`` of them,
      and only when the other side has more of the sentence;
    * a sentence ends where the whole-file text ends it *or* where the turn's
      own text does, so a run-on in either does not make it longer than it is.

    Words are moved, never changed or dropped. Nothing is done when the
    transcripts do not broadly agree. Returns the texts and how many
    sentences were put back together.
    """
    tokens: list[list[str]] = [_tokens(text) for text in texts]
    flat = [(_key(word), index) for index, turn in enumerate(tokens) for word in turn]
    full = _tokens(full_text)
    if not flat or not full:
        return list(texts), 0
    matcher = SequenceMatcher(
        None, [key for key, _ in flat], [_key(w) for w in full], autojunk=False
    )
    if matcher.ratio() < MIN_AGREEMENT:
        return list(texts), 0
    # Which sentence of the whole-file text each word belongs to (None: the
    # word has no counterpart there), and that text's spelling of it.
    sentence_of: list[int] = []
    number = 0
    for word in full:
        sentence_of.append(number)
        if _SENTENCE_END.search(word):
            number += 1
    mapped: list[int | None] = [None] * len(flat)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapped[block.a + offset] = block.b + offset
    # Per turn: (word, sentence or None, the whole-file text's word or None).
    words: list[list[tuple[str, int | None, str | None]]] = [[] for _ in tokens]
    position = 0
    for index, turn in enumerate(tokens):
        for word in turn:
            j = mapped[position]
            words[index].append(
                (word, None, None) if j is None else (word, sentence_of[j], full[j])
            )
            position += 1

    moved = 0
    heard = [index for index, turn in enumerate(words) if turn]
    for a, b in zip(heard, heard[1:], strict=False):
        if turns[a].speaker == turns[b].speaker or not words[a] or not words[b]:
            continue
        sentence = words[a][-1][1]
        if sentence is None or sentence != words[b][0][1]:
            continue  # the change of speaker is between two sentences
        # The sentence's last words in A: back to where it began.
        tail = 0
        for offset in range(len(words[a]) - 1, -1, -1):
            word, its, _full = words[a][offset]
            ended_here = offset < len(words[a]) - 1 and _SENTENCE_END.search(word)
            if (its is not None and its != sentence) or ended_here:
                break
            tail += 1
        # Its first words in B: up to and with the word that ends it.
        head = 0
        for word, its, theirs in words[b]:
            if its is not None and its != sentence:
                break
            head += 1
            if _SENTENCE_END.search(word) or (
                theirs is not None and _SENTENCE_END.search(theirs)
            ):
                break
        if tail == head or min(tail, head) > max_moved:
            continue
        if _SENTENCE_END.search(words[a][-1][0]):
            continue  # A's own text ends its sentence here
        if head < tail:
            # B's first words finish A's sentence.
            part, words[b] = words[b][:head], words[b][head:]
            first, its, theirs = part[0]
            # It continues the sentence: a small letter, unless the whole-file
            # text writes it with a capital (a name).
            if theirs is None or not theirs[:1].isupper():
                part[0] = (_set_first_letter(first, upper=False), its, theirs)
            last, its, theirs = words[a][-1]
            if theirs is not None:  # no full stop in the middle of it
                words[a][-1] = (
                    _TAIL.sub("", last) + _TAIL.search(theirs).group(),
                    its,
                    theirs,
                )
            words[a].extend(part)
            if words[b]:
                first, its, theirs = words[b][0]
                words[b][0] = (_set_first_letter(first, upper=True), its, theirs)
        else:
            # A's last words begin B's sentence.
            part, words[a] = words[a][-tail:], words[a][:-tail]
            first, its, theirs = words[b][0]
            if theirs is None or not theirs[:1].isupper():
                words[b][0] = (_set_first_letter(first, upper=False), its, theirs)
            last, its, theirs = part[-1]
            if theirs is not None:
                part[-1] = (
                    _TAIL.sub("", last) + _TAIL.search(theirs).group(),
                    its,
                    theirs,
                )
            words[b][0:0] = part
        moved += 1
    return [" ".join(word for word, _, _ in turn) for turn in words], moved
