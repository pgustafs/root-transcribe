"""A recording whose channels hold different sources.

In an online meeting the recorder keeps the microphone on the left channel and
the meeting tab on the right (``audio_layout == "mic_tab"``). Who is "here" and
who is "in the call" is then known from the channel, not guessed from the
voices: each channel is detected and transcribed on its own, and the two are
put back in order of time.

What can still go wrong is sound crossing over in the room: without headphones
the call comes out of the loudspeakers and into the microphone, where it would
be transcribed a second time under the wrong person. Such a stretch on the
microphone channel follows the tab channel's loudness, a moment later and
quieter, and is left out.

Pure logic on arrays of samples; no audio files and no models.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.services.transcript_align import SpeakerTurn

if TYPE_CHECKING:  # numpy lives in the speech worker's image only
    import numpy as np

#: Two channels are the same signal when what differs between them is at most
#: this share of their own level (a codec leaves a little difference).
SAME_SIGNAL_SHARE = 0.05
#: Loudness is followed in steps of this length (seconds).
FRAME_SECONDS = 0.02
#: An echo arrives at most this much later than the sound that caused it.
MAX_ECHO_DELAY_SECONDS = 0.3
#: A microphone stretch is an echo when its loudness follows the tab's this
#: closely (0-1) ...
ECHO_LIKENESS = 0.6
#: ... while the tab is sounding for at least this share of it ...
ECHO_TAB_ACTIVE_SHARE = 0.7
#: ... and it is at least this much quieter than the tab (decibels): a person
#: talking into their own microphone is not.
ECHO_QUIETER_DB = 6.0
#: The tab "is sounding" in a step that is within this many decibels of its
#: loud steps.
ACTIVE_BELOW_PEAK_DB = 30.0
#: Too short to judge by its loudness curve; kept.
MIN_JUDGED_SECONDS = 0.4


def channels_differ(left: np.ndarray, right: np.ndarray) -> bool:
    """Whether two channels really hold different signals.

    A recording said to be ``mic_tab`` is only treated so when they do: a file
    whose channels are copies of one mix (every recording made before the
    channels were kept apart) goes the ordinary way.
    """
    import numpy as np

    n = min(len(left), len(right))
    if n == 0:
        return False
    a, b = left[:n].astype(np.float64), right[:n].astype(np.float64)
    level = float(np.sqrt(np.mean(a * a)) + np.sqrt(np.mean(b * b)))
    if level <= 1e-6:
        return False  # silence on both
    return float(np.sqrt(np.mean((a - b) ** 2))) / level > SAME_SIGNAL_SHARE


def _loudness(samples: np.ndarray, frame: int) -> np.ndarray:
    """Root-mean-square level per step of ``frame`` samples."""
    import numpy as np

    n = len(samples) // frame
    if n == 0:
        return np.zeros(0)
    blocks = samples[: n * frame].astype(np.float64).reshape(n, frame)
    return np.sqrt(np.mean(blocks * blocks, axis=1))


def _likeness(a: np.ndarray, b: np.ndarray) -> float:
    """How alike two loudness curves are (correlation, 0 when either is flat)."""
    import numpy as np

    a, b = a - a.mean(), b - b.mean()
    scale = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
    return float(np.sum(a * b) / scale) if scale > 0 else 0.0


def is_echo(mic: np.ndarray, tab: np.ndarray, sample_rate: int) -> bool:
    """Whether a stretch of the microphone channel is the call heard through
    the loudspeakers: ``tab`` is the tab channel over the same stretch plus
    ``MAX_ECHO_DELAY_SECONDS`` before it.

    All three must hold: the tab is sounding for most of the stretch, the
    microphone is clearly quieter than it, and the microphone's loudness
    follows the tab's (at the delay where it follows best). Someone talking
    at the microphone while the call is also talking has a loudness curve of
    their own and is kept.
    """
    import numpy as np

    frame = max(1, int(FRAME_SECONDS * sample_rate))
    lead = int(MAX_ECHO_DELAY_SECONDS * sample_rate) // frame
    mic_level = _loudness(mic, frame)
    tab_level = _loudness(tab, frame)
    steps = len(mic_level)
    if steps * FRAME_SECONDS < MIN_JUDGED_SECONDS or len(tab_level) < steps + lead:
        return False
    aligned = tab_level[lead : lead + steps]
    peak = float(np.percentile(tab_level, 95))
    if peak <= 1e-5:
        return False  # nothing in the call to echo
    floor = peak * 10 ** (-ACTIVE_BELOW_PEAK_DB / 20)
    if float(np.mean(aligned > floor)) < ECHO_TAB_ACTIVE_SHARE:
        return False
    mic_rms = float(np.sqrt(np.mean(mic_level**2)))
    tab_rms = float(np.sqrt(np.mean(aligned**2)))
    if mic_rms <= 0 or 20 * np.log10(tab_rms / mic_rms) < ECHO_QUIETER_DB:
        return False
    # The echo is later than its cause: slide the tab back up to the limit.
    best = max(
        _likeness(mic_level, tab_level[lead - shift : lead - shift + steps])
        for shift in range(lead + 1)
    )
    return best >= ECHO_LIKENESS


def drop_echo(
    turns: Sequence[SpeakerTurn],
    mic: np.ndarray,
    tab: np.ndarray,
    sample_rate: int,
) -> tuple[list[SpeakerTurn], int]:
    """The microphone channel's turns without those that are the call's echo.
    Returns the turns kept and how many were left out."""
    import numpy as np

    lead = int(MAX_ECHO_DELAY_SECONDS * sample_rate)
    kept: list[SpeakerTurn] = []
    for turn in turns:
        start = max(0, int(turn.start * sample_rate))
        end = min(len(mic), int(turn.end * sample_rate))
        before = max(0, start - lead)
        # The tab stretch starts ``lead`` earlier; pad when the recording
        # begins inside that margin so the two stay lined up.
        tab_part = tab[before : min(len(tab), end)]
        if start - before < lead:
            tab_part = np.concatenate([np.zeros(lead - (start - before)), tab_part])
        if is_echo(mic[start:end], tab_part, sample_rate):
            continue
        kept.append(turn)
    return kept, len(turns) - len(kept)


def relabel(
    turns: Sequence[SpeakerTurn], first: int
) -> tuple[list[SpeakerTurn], dict[str, str]]:
    """Give a channel's speakers the labels ``SPEAKER_<first>``, ``<first+1>``
    ... in order of who speaks first, so the labels of two channels never
    collide. Returns the turns and the old-to-new mapping."""
    mapping: dict[str, str] = {}
    for turn in sorted(turns, key=lambda t: (t.start, t.end)):
        if turn.speaker not in mapping:
            mapping[turn.speaker] = f"SPEAKER_{first + len(mapping):02d}"
    return (
        [SpeakerTurn(mapping[t.speaker], t.start, t.end) for t in turns],
        mapping,
    )
