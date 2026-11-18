"""Speaker diarization + speaker embeddings (pyannote) — the heavy-ML service.

This module runs ONLY in the stt-worker image (deploy/stt.Containerfile, the
optional ``stt`` deps). Every heavy import (torch, pyannote.audio, soundfile)
is **inside** a method, so importing this module in the base API / test environment
is cheap and safe — the pyannote models load lazily on first use in the worker and
are reused (a per-process singleton), and need ``HF_TOKEN`` for the gated weights.

- :class:`DiarizationService` → who spoke when (``SpeakerTurn`` list).
- :class:`SpeakerEmbeddingService` → a fixed-length voice embedding per speaker
  (for identification) or for a whole clip (for enrollment).

See ARCHITECTURE.md.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable

from app.core.config import settings
from app.core.logging import log_operation
from app.services.transcript_align import SpeakerTurn, absorb_slivers

logger = logging.getLogger("app.services.diarization")


def decode_to_wav(audio: bytes, *, src_hint: str = "audio") -> str:
    """Decode arbitrary audio bytes (webm/opus, mp4, wav…) to a mono 16 kHz WAV.

    Returns a temp file path the caller is responsible for deleting. Uses the
    ffmpeg binary bundled by the ``imageio-ffmpeg`` wheel (an ``[stt]`` dep), so
    the image needs **no** system ffmpeg / RPM Fusion / EPEL. 16 kHz mono is what
    the diarization and embedding models expect.
    """
    import imageio_ffmpeg  # bundles a static ffmpeg — worker/[stt] only

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    suffix = "." + (src_hint.rsplit(".", 1)[-1] if "." in src_hint else "webm")
    ifd, in_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(ifd, "wb") as f:
        f.write(audio)
    ofd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(ofd)
    try:
        with log_operation(logger, "audio.decode_wav", bytes=len(audio), src=src_hint):
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-i", in_path, "-ac", "1", "-ar", "16000", out_path],
                    check=True,
                    capture_output=True,
                    # A malformed/hostile sample must not hang the worker (issue #14
                    # review): bound the decode.
                    timeout=120,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                # Don't let the raw CalledProcessError (its cmd = ffmpeg executable
                # + temp paths) reach the operator log via log_operation's exc_info.
                # `from None` drops the chain so exc_info can't leak it (issue #37).
                raise RuntimeError("audio decode failed") from None
    except BaseException:
        # A failed/timed-out decode raises before the caller receives out_path,
        # so it can never clean it — remove the partial output here or repeated
        # bad audio exhausts the worker's tmp dir (issue #14 review).
        with contextlib.suppress(OSError):
            os.remove(out_path)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.remove(in_path)
    return out_path


def decode_file_to_wav(
    in_path: str, *, src_hint: str = "audio", max_seconds: float | None = None
) -> str:
    """File→file decode to mono 16 kHz WAV (issue #6): the compressed input is
    never held in memory — it streams from disk through ffmpeg to disk.

    ``max_seconds`` bounds the DECODE itself (ffmpeg ``-t``): a tiny hostile
    input claiming hours of audio can expand to at most the cap (+1 s so the
    post-decode duration probe still detects and rejects an over-limit
    recording) instead of filling the disk before the guard runs.
    """
    import imageio_ffmpeg

    from app.services import media_validation

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    ofd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(ofd)
    cmd = [ffmpeg, "-y", "-i", in_path, "-ac", "1", "-ar", "16000"]
    if max_seconds is not None:
        cmd += ["-t", f"{max_seconds + 1:.0f}"]
    cmd.append(out_path)
    try:
        with log_operation(logger, "audio.decode_wav_file", src=src_hint):
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                # Classify + sanitize INSIDE the log_operation block so its
                # exception logging never sees the raw CalledProcessError (cmd =
                # ffmpeg executable + temp paths). `from None` drops the chain so
                # exc_info can't leak it either (issue #37 review). Callers get a
                # typed MediaValidationError with only the safe, sanitized detail.
                raise media_validation.classify_decode_failure(exc) from None
    except BaseException:
        # Clean the partial output on failure/timeout — the caller never gets
        # the path, so it cannot (issue #14 review).
        with contextlib.suppress(OSError):
            os.remove(out_path)
        raise
    return out_path


def decode_file_to_channel_wavs(
    in_path: str, *, max_seconds: float | None = None
) -> tuple[str, str]:
    """File→files decode of a stereo recording into two mono 16 kHz WAVs,
    (left, right): the two sources of an online-meeting recording, kept apart
    (see ``channel_audio``). Bounded like :func:`decode_file_to_wav`.

    A mono input gives the same signal twice; the caller checks that the
    channels differ before treating them as two sources.
    """
    import imageio_ffmpeg

    from app.services import media_validation

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    paths = []
    for _ in range(2):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        paths.append(path)
    limit = ["-t", f"{max_seconds + 1:.0f}"] if max_seconds is not None else []
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        in_path,
        "-filter_complex",
        # Whatever the input layout, lay it out as stereo first, then split.
        "[0:a]aformat=channel_layouts=stereo,channelsplit=channel_layout=stereo[l][r]",
    ]
    for label, path in zip(("[l]", "[r]"), paths, strict=True):
        cmd += ["-map", label, "-ar", "16000", *limit, path]
    try:
        with log_operation(logger, "audio.decode_channel_wavs"):
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                # Sanitized like decode_file_to_wav: never the ffmpeg command.
                raise media_validation.classify_decode_failure(exc) from None
    except BaseException:
        for path in paths:
            with contextlib.suppress(OSError):
                os.remove(path)
        raise
    return paths[0], paths[1]


def probe_wav(wav_path: str) -> tuple[float, int]:
    """(duration_seconds, sample_rate) from the WAV header only — the
    post-decode guard that enforces duration limits BEFORE anything loads the
    waveform into memory (zip-bomb defense, issue #6)."""
    import soundfile

    info = soundfile.info(wav_path)
    return float(info.frames) / float(info.samplerate), int(info.samplerate)


def slice_wav(wav_path: str, start: float, end: float, pad: float = 0.0) -> bytes:
    """Extract ``[start, end)`` seconds of a WAV as standalone WAV bytes, with
    ``pad`` seconds of silence added at both ends (a speech model handles a
    clip that begins and ends in silence better than one cut mid-sound).

    Used by the per-turn transcription fallback: when the STT server returns no
    word timestamps, each diarized turn's audio is transcribed individually, so
    speaker attribution never depends on (drifting) STT timestamps. soundfile
    reads only the requested frames — no full-file copy per slice.
    """
    import io

    import soundfile as sf

    with sf.SoundFile(wav_path) as f:
        sr = f.samplerate
        first = max(0, int(start * sr))
        last = min(len(f), int(end * sr))
        f.seek(first)
        data = f.read(frames=max(0, last - first), dtype="float32", always_2d=True)
    if pad > 0:
        import numpy as np

        silence = np.zeros((int(pad * sr), data.shape[1]), dtype="float32")
        data = np.concatenate([silence, data, silence])
    buf = io.BytesIO()
    sf.write(buf, data, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def save_wav(wav_path: str, samples, sample_rate: int) -> None:
    """Write mono float samples over a 16-bit WAV (a channel with stretches
    silenced, for the speech model to read)."""
    import soundfile as sf

    sf.write(wav_path, samples, sample_rate, subtype="PCM_16")


def load_waveform(wav_path: str) -> dict:
    """Load a WAV into a pyannote audio dict.

    Returns ``{"waveform": (channel, time) float32 Tensor, "sample_rate": int}``.
    Passing pyannote a waveform dict (instead of a file path) sidesteps its
    pyannote-4.x file reader, which needs ``torchcodec`` + system ffmpeg
    libraries; ``soundfile`` reads the WAV directly (libsndfile is bundled in its
    wheel), so the stt image stays free of system audio packages.
    """
    import soundfile as sf
    import torch

    data, sr = sf.read(wav_path, dtype="float32", always_2d=True)  # (time, channel)
    waveform = torch.from_numpy(data.T).contiguous()  # (channel, time)
    return {"waveform": waveform, "sample_rate": int(sr)}


class DiarizationService:
    """pyannote speaker-diarization pipeline (lazy singleton)."""

    def __init__(self) -> None:
        self._pipeline = None
        self._speech = None

    def speech_sync(
        self,
        audio: dict,
        *,
        label: str = "SPEAKER_00",
        on_progress: Callable[[], None] | None = None,
    ) -> list[SpeakerTurn]:
        """Where someone is speaking, as turns of one speaker.

        For audio known to hold one person (a count of 1, or one side of an
        online meeting): the question is only *when* there is speech, which the
        pipeline's own segmentation model answers by itself. Telling voices
        apart is the slow part of speaker detection (measured on a 42-minute
        meeting: about 28 minutes on CPU, against 2 for this) and is skipped.
        """
        if self._speech is None:
            from pyannote.audio.pipelines import VoiceActivityDetection

            pipeline = self._load()
            with log_operation(logger, "diarization.load_speech_detection"):
                speech = VoiceActivityDetection(
                    segmentation=pipeline._segmentation.model
                )
                speech.instantiate({"min_duration_on": 0.0, "min_duration_off": 0.0})
                self._speech = speech
        with log_operation(logger, "diarization.speech"):
            regions = self._speech(audio, **_hook(on_progress)).get_timeline().support()
        return [
            SpeakerTurn(speaker=label, start=float(r.start), end=float(r.end))
            for r in regions
        ]

    def _load(self):
        if self._pipeline is None:
            import torch
            from pyannote.audio import Pipeline

            with log_operation(logger, "diarization.load_model"):
                # pyannote.audio 4.x renamed the 3.x ``use_auth_token`` kwarg to
                # ``token``.
                pipeline = Pipeline.from_pretrained(
                    settings.DIARIZATION_MODEL,
                    token=settings.HF_TOKEN or None,
                )
                # CPU by default; honour CUDA when the worker has a GPU.
                if torch.cuda.is_available():
                    pipeline.to(torch.device("cuda"))
                self._pipeline = pipeline
        return self._pipeline

    def diarize_sync(
        self,
        audio: dict,
        *,
        num_speakers: int | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> list[SpeakerTurn]:
        """Return chronologically-sorted speaker turns for a loaded waveform dict.

        ``num_speakers``: how many people speak, when the user knows it. Left
        out, the pipeline estimates the count, and a wrong estimate invents a
        speaker out of quiet, distant or overlapping speech.

        ``on_progress``: called again and again while the pipeline works (it
        can take as long as the recording itself), so the caller can show that
        it is still alive."""
        pipeline = self._load()
        options = {"num_speakers": num_speakers} if num_speakers else {}
        options.update(_hook(on_progress))
        with log_operation(logger, "diarization.run", num_speakers=num_speakers):
            output = pipeline(audio, **options)
        # pyannote 4.x community pipelines return a ``DiarizeOutput`` wrapper;
        # older pipelines return the ``Annotation`` directly. Prefer the wrapper's
        # EXCLUSIVE diarization (one speaker at a time): the regular one allows
        # overlapping turns, and a short overlapped turn transcribed on its own
        # repeats the main speaker's words under a second, non-existent speaker.
        annotation = getattr(output, "exclusive_speaker_diarization", None)
        if annotation is None:
            annotation = getattr(output, "speaker_diarization", output)
        turns = [
            SpeakerTurn(speaker=str(label), start=float(seg.start), end=float(seg.end))
            for seg, _, label in annotation.itertracks(yield_label=True)
        ]
        return absorb_slivers(turns)


def _hook(on_progress: Callable[[], None] | None) -> dict:
    """``on_progress`` as the pipeline's ``hook`` option (pyannote calls it
    with every batch of each step); nothing when there is none, so the
    pipeline is called exactly as before."""
    if on_progress is None:
        return {}

    def hook(*args: object, **kwargs: object) -> None:
        on_progress()

    return {"hook": hook}


class SpeakerEmbeddingService:
    """pyannote speaker-embedding model (lazy singleton)."""

    def __init__(self) -> None:
        self._inference = None
        self._model_id = settings.SPEAKER_EMBEDDING_MODEL

    def _load(self):
        if self._inference is None:
            import torch
            from pyannote.audio import Inference, Model

            with log_operation(logger, "embedding.load_model"):
                model = Model.from_pretrained(
                    settings.SPEAKER_EMBEDDING_MODEL,
                    token=settings.HF_TOKEN or None,
                )
                # pyannote's Inference expects a torch.device, not a string
                # (it reads ``device.type`` internally).
                device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                self._inference = Inference(model, window="whole", device=device)
        return self._inference

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed_clip_sync(self, audio: dict) -> list[float]:
        """Embed a whole clip (used for enrollment)."""
        inference = self._load()
        with log_operation(logger, "embedding.clip"):
            vector = inference(audio)
        return _to_list(vector)

    def embed_region_sync(self, audio: dict, start: float, end: float) -> list[float]:
        """Embed a time region (used to identify a diarized speaker)."""
        from pyannote.core import Segment

        inference = self._load()
        vector = inference.crop(audio, Segment(start, end))
        return _to_list(vector)

    def embed_speakers_sync(
        self, audio: dict, turns: list[SpeakerTurn]
    ) -> dict[str, list[float]]:
        """One embedding per diarized speaker, from their longest clean turn.

        The longest single turn is the most reliable single-speaker region, so we
        embed that rather than averaging across scattered short turns.
        """
        longest: dict[str, SpeakerTurn] = {}
        for turn in turns:
            cur = longest.get(turn.speaker)
            if cur is None or (turn.end - turn.start) > (cur.end - cur.start):
                longest[turn.speaker] = turn
        out: dict[str, list[float]] = {}
        for speaker, turn in longest.items():
            try:
                out[speaker] = self.embed_region_sync(audio, turn.start, turn.end)
            except Exception:  # noqa: BLE001 — a bad region just stays anonymous
                logger.warning("could not embed speaker %s", speaker)
        return out


def _to_list(vector) -> list[float]:
    """Convert a numpy embedding to a plain float list (drops the ML dep at rest)."""
    tolist = getattr(vector, "tolist", None)
    if callable(tolist):
        flat = vector.reshape(-1).tolist() if hasattr(vector, "reshape") else tolist()
        return [float(x) for x in flat]
    return [float(x) for x in vector]


_diarization: DiarizationService | None = None
_embedding: SpeakerEmbeddingService | None = None


def get_diarization_service() -> DiarizationService:
    global _diarization
    if _diarization is None:
        _diarization = DiarizationService()
    return _diarization


def get_embedding_service() -> SpeakerEmbeddingService:
    global _embedding
    if _embedding is None:
        _embedding = SpeakerEmbeddingService()
    return _embedding
