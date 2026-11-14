"""Pure alignment logic: word-timestamps × diarization turns → speaker segments.

When ASR supplies word timestamps, assign each word to the speaker turn with
most overlap, then merge adjacent words for the same speaker. The worker uses
per-turn transcription instead when the ASR response lacks word timestamps.

Everything here uses only the standard library and runs without model loading.
``tasks.transcribe_attempt`` supplies the STT and diarization output and persists
the aligned segments.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    """One transcribed word with its start/end time in seconds."""

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class SpeakerTurn:
    """A diarized span attributed to one speaker, in seconds."""

    speaker: str
    start: float
    end: float


def absorb_slivers(
    turns: Sequence[SpeakerTurn], *, max_sliver: float = 0.5, max_gap: float = 0.1
) -> list[SpeakerTurn]:
    """Fold a brief other-speaker blip inside one person's speech back into it.

    Diarization sometimes labels a fraction of a second in the middle of one
    person's sentence as another speaker. Transcribed on its own, that blip
    becomes a garbage line under a speaker who does not exist. A turn of at
    most ``max_sliver`` seconds that sits directly between two turns of the same
    other speaker (gaps of at most ``max_gap``) is treated as that speaker's
    speech: the three turns become one. A short turn with a pause around it, or
    between two different speakers, is a real interjection and is kept.
    """
    out: list[SpeakerTurn] = []
    ordered = sorted(turns, key=lambda t: (t.start, t.end))
    i = 0
    while i < len(ordered):
        turn = ordered[i]
        if out and i + 1 < len(ordered):
            prev, nxt = out[-1], ordered[i + 1]
            if (
                prev.speaker == nxt.speaker != turn.speaker
                and turn.end - turn.start <= max_sliver
                and turn.start - prev.end <= max_gap
                and nxt.start - turn.end <= max_gap
            ):
                out[-1] = SpeakerTurn(prev.speaker, prev.start, max(prev.end, nxt.end))
                i += 2
                continue
        out.append(turn)
        i += 1
    return out


@dataclass(frozen=True)
class Segment:
    """A contiguous same-speaker span of transcript."""

    speaker: str
    start: float
    end: float
    text: str


def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Length of the temporal overlap between two intervals (0 if disjoint)."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speaker(word: Word, turns: Sequence[SpeakerTurn]) -> str | None:
    """The speaker whose turn overlaps ``word`` most.

    Falls back to the nearest turn (by gap to the word midpoint) when a word sits
    in a diarization gap, so no word is dropped. Returns ``None`` only when there
    are no turns at all.
    """
    if not turns:
        return None
    best_speaker: str | None = None
    best_overlap = 0.0
    for turn in turns:
        ov = overlap(word.start, word.end, turn.start, turn.end)
        if ov > best_overlap:
            best_overlap = ov
            best_speaker = turn.speaker
    if best_speaker is not None:
        return best_speaker
    # No overlap with any turn — attach to the temporally nearest turn.
    midpoint = (word.start + word.end) / 2
    nearest = min(
        turns,
        key=lambda t: (
            0.0
            if t.start <= midpoint <= t.end
            else min(abs(midpoint - t.start), abs(midpoint - t.end))
        ),
    )
    return nearest.speaker


def align(words: Sequence[Word], turns: Sequence[SpeakerTurn]) -> list[Segment]:
    """Assign every word a speaker, then merge consecutive same-speaker words.

    The merge is on *adjacent* words only, so a speaker who talks, yields, then
    talks again yields two segments (chronological order preserved). Word text is
    joined with single spaces and stripped.
    """
    segments: list[Segment] = []
    cur_speaker: str | None = None
    cur_words: list[Word] = []

    def flush() -> None:
        if not cur_words or cur_speaker is None:
            return
        text = " ".join(w.text.strip() for w in cur_words if w.text.strip())
        if text:
            segments.append(
                Segment(
                    speaker=cur_speaker,
                    start=cur_words[0].start,
                    end=cur_words[-1].end,
                    text=text,
                )
            )

    for word in words:
        speaker = assign_speaker(word, turns) or (cur_speaker or "SPEAKER_00")
        if speaker != cur_speaker:
            flush()
            cur_speaker = speaker
            cur_words = []
        cur_words.append(word)
    flush()
    return segments
