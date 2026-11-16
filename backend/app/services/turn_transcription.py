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


def _key(word: str) -> str:
    return re.sub(r"[^\w]+", "", word, flags=re.UNICODE).casefold()


def _words(text: str) -> list[str]:
    return [word for word in text.split() if _key(word)]


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
    words: list[list[str]] = [_words(text) for text in texts]
    flat: list[tuple[str, int]] = [
        (_key(word), index) for index, turn in enumerate(words) for word in turn
    ]
    full = _words(full_text)
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
            words[target][0:0] = run
        else:
            words[target].extend(run)
        added += 1
    return [" ".join(turn) for turn in words], added
