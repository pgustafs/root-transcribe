"""Unit tests for word↔speaker alignment and transcript rendering."""

from __future__ import annotations

import pytest

from app.services.transcript_align import (
    Segment,
    SpeakerTurn,
    Word,
    absorb_slivers,
    align,
    assign_speaker,
    overlap,
)

pytestmark = pytest.mark.unit


def test_overlap() -> None:
    assert overlap(0, 2, 1, 3) == 1
    assert overlap(0, 1, 2, 3) == 0
    assert overlap(0, 5, 1, 2) == 1


def test_assign_speaker_picks_max_overlap() -> None:
    turns = [
        SpeakerTurn("SPEAKER_00", 0.0, 2.0),
        SpeakerTurn("SPEAKER_01", 2.0, 4.0),
    ]
    assert assign_speaker(Word("hi", 0.1, 0.5), turns) == "SPEAKER_00"
    assert assign_speaker(Word("bye", 3.0, 3.5), turns) == "SPEAKER_01"


def test_assign_speaker_falls_back_to_nearest_in_a_gap() -> None:
    turns = [
        SpeakerTurn("SPEAKER_00", 0.0, 1.0),
        SpeakerTurn("SPEAKER_01", 5.0, 6.0),
    ]
    # A word in the 1–5s gap attaches to the nearest turn, not dropped.
    assert assign_speaker(Word("uh", 1.4, 1.6), turns) == "SPEAKER_00"
    assert assign_speaker(Word("hm", 4.6, 4.8), turns) == "SPEAKER_01"


def test_assign_speaker_no_turns_is_none() -> None:
    assert assign_speaker(Word("x", 0, 1), []) is None


def test_align_merges_consecutive_same_speaker_words() -> None:
    turns = [
        SpeakerTurn("SPEAKER_00", 0.0, 2.0),
        SpeakerTurn("SPEAKER_01", 2.0, 4.0),
    ]
    words = [
        Word("Hello", 0.0, 0.5),
        Word("there", 0.6, 1.0),
        Word("Hi", 2.1, 2.5),
        Word("back", 2.6, 3.0),
    ]
    segments = align(words, turns)
    assert segments == [
        Segment("SPEAKER_00", 0.0, 1.0, "Hello there"),
        Segment("SPEAKER_01", 2.1, 3.0, "Hi back"),
    ]


def test_align_splits_when_speaker_returns() -> None:
    turns = [
        SpeakerTurn("SPEAKER_00", 0.0, 1.0),
        SpeakerTurn("SPEAKER_01", 1.0, 2.0),
        SpeakerTurn("SPEAKER_00", 2.0, 3.0),
    ]
    words = [
        Word("a", 0.1, 0.4),
        Word("b", 1.1, 1.4),
        Word("c", 2.1, 2.4),
    ]
    segments = align(words, turns)
    assert [s.speaker for s in segments] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
    ]


def test_align_empty_words() -> None:
    assert align([], [SpeakerTurn("SPEAKER_00", 0, 1)]) == []


def test_absorb_slivers_folds_a_blip_inside_one_speaker() -> None:
    turns = [
        SpeakerTurn("SPEAKER_00", 0.0, 3.0),
        SpeakerTurn("SPEAKER_01", 3.0, 3.4),
        SpeakerTurn("SPEAKER_00", 3.4, 7.0),
    ]
    assert absorb_slivers(turns) == [SpeakerTurn("SPEAKER_00", 0.0, 7.0)]


def test_absorb_slivers_keeps_real_interjections() -> None:
    paused = [  # the first speaker stops; a pause surrounds the reply
        SpeakerTurn("SPEAKER_00", 0.0, 3.0),
        SpeakerTurn("SPEAKER_01", 3.4, 3.8),
        SpeakerTurn("SPEAKER_00", 4.3, 7.0),
    ]
    assert absorb_slivers(paused) == paused
    longer = [  # too long to be a blip
        SpeakerTurn("SPEAKER_00", 0.0, 3.0),
        SpeakerTurn("SPEAKER_01", 3.0, 4.0),
        SpeakerTurn("SPEAKER_00", 4.0, 7.0),
    ]
    assert absorb_slivers(longer) == longer
    between_others = [  # short, but between two different speakers
        SpeakerTurn("SPEAKER_00", 0.0, 3.0),
        SpeakerTurn("SPEAKER_01", 3.0, 3.3),
        SpeakerTurn("SPEAKER_02", 3.3, 7.0),
    ]
    assert absorb_slivers(between_others) == between_others


def test_diarization_prefers_exclusive_turns() -> None:
    from types import SimpleNamespace

    from app.services.diarization import DiarizationService

    def annotation(*tracks):
        return SimpleNamespace(
            itertracks=lambda yield_label: [
                (SimpleNamespace(start=a, end=b), None, label) for a, b, label in tracks
            ]
        )

    service = DiarizationService()
    service._pipeline = lambda audio: SimpleNamespace(
        speaker_diarization=annotation(
            (0.0, 7.0, "SPEAKER_00"), (0.1, 0.6, "SPEAKER_01"), (3.0, 3.6, "SPEAKER_01")
        ),
        exclusive_speaker_diarization=annotation((0.0, 7.0, "SPEAKER_00")),
    )
    assert service.diarize_sync({}) == [SpeakerTurn("SPEAKER_00", 0.0, 7.0)]
    # Older pipelines return the annotation itself.
    service._pipeline = lambda audio: annotation((1.0, 2.0, "SPEAKER_00"))
    assert service.diarize_sync({}) == [SpeakerTurn("SPEAKER_00", 1.0, 2.0)]
