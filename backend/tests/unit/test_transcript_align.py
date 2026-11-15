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


# --- cutting and rescuing speaker turns ------------------------------------------


def test_clip_gets_a_margin_but_never_a_neighbours_speech() -> None:
    from app.services.turn_transcription import clip_bounds, context_bounds

    turns = [
        SpeakerTurn("A", 0.1, 2.0),
        SpeakerTurn("B", 2.1, 4.0),  # 0.1 s after A: half of that each
        SpeakerTurn("A", 5.0, 9.9),
    ]
    assert clip_bounds(turns, 0, 10.0) == (0.0, 2.05)
    start, end = clip_bounds(turns, 1, 10.0)
    assert (round(start, 2), round(end, 2)) == (2.05, 4.2)
    # The end of the recording limits the last margin.
    start, end = clip_bounds(turns, 2, 10.0)
    assert (round(start, 2), round(end, 2)) == (4.8, 10.0)
    # A turn that ends a rounding step past the header's duration is not cut.
    assert clip_bounds([SpeakerTurn("A", 0.0, 70.0)], 0, 40.0) == (0.0, 70.0)
    assert context_bounds(SpeakerTurn("B", 1.9, 2.4), 39.0) == (0.0, 4.9)
    assert context_bounds(SpeakerTurn("B", 38.0, 38.5), 39.0) == (35.5, 39.0)


def test_rescue_takes_the_neighbours_words_out() -> None:
    from app.services.turn_transcription import rescue_text

    # The reply between two turns: both neighbours are in the context.
    assert (
        rescue_text(
            "bra, men lite trött. Hur mår du? Jag mår också bra.",
            "Jag mår bra men lite trött.",
            "Jag mår också bra.",
            turn_seconds=0.8,
        )
        == "Hur mår du?"
    )
    # The same word as the neighbour: the repetition is what is left.
    assert (
        rescue_text(
            "Hej! Hej! Jag heter Peter.",
            "Hej!",
            "Jag heter Peter. Vad heter du?",
            turn_seconds=0.5,
        )
        == "Hej!"
    )
    # Nothing but the neighbours was heard: there was no speech in the turn.
    assert (
        rescue_text("Jag mår också bra.", "", "Jag mår också bra.", turn_seconds=1.0)
        == ""
    )
    assert rescue_text("", "a", "b", turn_seconds=1.0) == ""
    # The neighbours were not recognised: their text is not passed off as the
    # short turn's.
    assert (
        rescue_text(
            "this is a long sentence that belongs to somebody else entirely",
            "Hej!",
            "Okej.",
            turn_seconds=0.5,
        )
        == ""
    )


def test_slice_wav_adds_silence_at_both_ends(tmp_path) -> None:
    import io

    # Both live in the speech worker's image, not in the API's environment.
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")

    from app.services.diarization import slice_wav

    path = tmp_path / "a.wav"
    sf.write(path, np.full(16000, 0.5, dtype="float32"), 16000)  # one second
    data, rate = sf.read(io.BytesIO(slice_wav(str(path), 0.25, 0.75, 0.3)))
    assert rate == 16000 and len(data) == 8000 + 2 * 4800
    assert not data[:4800].any() and not data[-4800:].any()
    assert abs(float(data[4800:-4800].mean()) - 0.5) < 0.01
    # Without padding the clip is the cut itself.
    plain, _ = sf.read(io.BytesIO(slice_wav(str(path), 0.25, 0.75)))
    assert len(plain) == 8000


def test_same_speaker_turns_with_almost_no_pause_are_joined() -> None:
    from app.services.turn_transcription import join_close_turns

    turns = [
        SpeakerTurn("A", 15.0, 19.6),  # "... mellan Stockholm"
        SpeakerTurn("A", 19.9, 21.5),  # "och Sälen med bil"
        SpeakerTurn("B", 23.0, 24.0),
        SpeakerTurn("A", 24.2, 25.0),  # another speaker in between: kept apart
        SpeakerTurn("A", 25.5, 26.0),  # half a second or more: a real pause
    ]
    assert join_close_turns(turns) == [
        SpeakerTurn("A", 15.0, 21.5),
        SpeakerTurn("B", 23.0, 24.0),
        SpeakerTurn("A", 24.2, 25.0),
        SpeakerTurn("A", 25.5, 26.0),
    ]
    # A long talk is not chained into one endless turn: a joined turn stays
    # within one 30-second request.
    talk = [SpeakerTurn("A", n * 10.0, n * 10.0 + 9.8) for n in range(5)]
    assert join_close_turns(talk) == [
        SpeakerTurn("A", 0.0, 29.8),
        SpeakerTurn("A", 30.0, 49.8),
    ]
    # Order does not matter, and nothing is lost.
    assert join_close_turns(list(reversed(turns))) == join_close_turns(turns)
    assert join_close_turns([]) == []
