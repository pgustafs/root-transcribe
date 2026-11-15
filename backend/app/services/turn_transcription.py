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
#: Same-speaker turns with a shorter pause than this are one stretch of speech.
JOIN_GAP_SECONDS = 0.5
#: A joined turn stays within one request to the model (its 30 s window), so
#: joining never creates a cut in the middle of a word.
JOIN_MAX_SECONDS = 30.0


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
