"""A recording whose channels hold different sources (microphone, meeting tab)."""

from __future__ import annotations

import pytest

from app.services.transcript_align import SpeakerTurn

np = pytest.importorskip("numpy")

from app.services.channel_audio import (  # noqa: E402
    channels_differ,
    drop_echo,
    is_echo,
    mute,
    relabel,
)

RATE = 16000


def voice(seconds: float, seed: int, level: float = 0.2):
    """Something with the shape of speech: noise whose loudness rises and
    falls in syllables, differently for every ``seed``."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    syllables = rng.uniform(0.05, 1.0, size=max(2, int(seconds * 5) + 2))
    envelope = np.interp(
        np.linspace(0, len(syllables) - 1, n), np.arange(len(syllables)), syllables
    )
    return (rng.standard_normal(n) * envelope * level).astype("float32")


def silence(seconds: float):
    return np.zeros(int(seconds * RATE), dtype="float32")


def echo_of(signal, *, delay: float = 0.08, quieter_db: float = 20.0):
    """``signal`` as a room returns it: later, quieter, a little smeared."""
    shift = int(delay * RATE)
    late = np.concatenate([np.zeros(shift, dtype="float32"), signal[:-shift]])
    smear = np.convolve(late, np.ones(160) / 160, mode="same")
    return ((0.7 * late + 0.3 * smear) * 10 ** (-quieter_db / 20)).astype("float32")


def test_channels_that_are_one_mix_are_not_two_sources() -> None:
    a, b = voice(3.0, 1), voice(3.0, 2)
    assert channels_differ(a, b)
    # The same signal on both (what every older recording has), also after a
    # codec left a trace of difference.
    assert not channels_differ(a, a.copy())
    assert not channels_differ(a, a + 0.001 * voice(3.0, 3))
    # Speech on one side only is as different as it gets.
    assert channels_differ(a, silence(3.0))
    # Nothing on either side is nothing to tell apart.
    assert not channels_differ(silence(1.0), silence(1.0))
    assert not channels_differ(np.zeros(0), np.zeros(0))


def test_the_call_heard_through_the_loudspeakers_is_an_echo() -> None:
    lead = int(0.3 * RATE)
    for seed in range(8):
        call = voice(3.3, seed)
        # The call comes back into the microphone, later and quieter.
        assert is_echo(echo_of(call)[lead:], call, RATE), seed
        # Also from a room that returns it slower or louder.
        assert is_echo(echo_of(call, delay=0.2, quieter_db=10)[lead:], call, RATE)
        # Someone here talking while the call is talking is not an echo ...
        own = voice(3.0, 100 + seed)
        assert not is_echo(own, call, RATE), seed
        # ... also when they talk quietly, or over the echo itself.
        assert not is_echo(own * 0.1, call, RATE), seed
        assert not is_echo(own + echo_of(call)[lead:], call, RATE), seed
    # Nothing in the call: there is nothing to echo.
    assert not is_echo(voice(3.0, 5), silence(3.3), RATE)
    # Too short to tell by its loudness: kept.
    short = voice(0.6, 9)
    assert not is_echo(echo_of(short)[lead:], short, RATE)


def test_echo_turns_are_left_out_and_real_ones_kept() -> None:
    # The call: speech at 1-4 s and 10-13 s. Here: the first comes back as an
    # echo; someone answers at 5-8 s; at 10-13 s someone talks over the call.
    call = np.concatenate(
        [silence(1), voice(3, 1), silence(6), voice(3, 2), silence(2)]
    )
    mic = echo_of(call)
    mic[5 * RATE : 8 * RATE] += voice(3, 3)
    mic[10 * RATE : 13 * RATE] += voice(3, 4)
    turns = [
        SpeakerTurn("SPEAKER_00", 1.1, 4.0),  # echo
        SpeakerTurn("SPEAKER_00", 5.0, 8.0),  # the answer
        SpeakerTurn("SPEAKER_00", 10.0, 13.0),  # talking over the call
        SpeakerTurn("SPEAKER_00", 14.0, 14.3),  # a blip
    ]
    kept, dropped = drop_echo(turns, mic, call, RATE)
    assert dropped == turns[:1]
    assert kept == turns[1:]
    # The echo is silenced in the audio as well, so nothing that listens to
    # the whole channel later hears the call there; the rest is untouched.
    before = mic.copy()
    mute(mic, dropped, RATE)
    assert not mic[int(1.1 * RATE) : 4 * RATE].any()
    assert (mic[5 * RATE : 8 * RATE] == before[5 * RATE : 8 * RATE]).all()
    assert (mic[: int(1.1 * RATE)] == before[: int(1.1 * RATE)]).all()
    # A turn at the very start of the recording is judged like any other.
    early = np.concatenate([voice(2, 6), silence(1)])
    start = [SpeakerTurn("SPEAKER_00", 0.1, 2.0)]
    assert drop_echo(start, echo_of(early), early, RATE) == ([], start)
    assert drop_echo([], mic, call, RATE) == ([], [])


def test_speakers_of_two_channels_get_labels_that_do_not_collide() -> None:
    mic = [SpeakerTurn("SPEAKER_00", 0.0, 2.0), SpeakerTurn("SPEAKER_00", 5.0, 6.0)]
    tab = [
        SpeakerTurn("SPEAKER_01", 2.5, 4.0),  # speaks first: the first new label
        SpeakerTurn("SPEAKER_00", 6.5, 8.0),
        SpeakerTurn("SPEAKER_01", 8.0, 9.0),
    ]
    mic, mic_labels = relabel(mic, 0)
    tab, tab_labels = relabel(tab, len(mic_labels))
    assert mic_labels == {"SPEAKER_00": "SPEAKER_00"}
    assert tab_labels == {"SPEAKER_01": "SPEAKER_01", "SPEAKER_00": "SPEAKER_02"}
    assert [t.speaker for t in tab] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_01"]
    assert [(t.start, t.end) for t in tab] == [(2.5, 4.0), (6.5, 8.0), (8.0, 9.0)]
    assert relabel([], 3) == ([], {})
