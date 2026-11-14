"""OpenAI-compatible speech-to-text client for the dedicated STT_BASE_URL.

Requests word timestamps, with segment-timestamp fallback for the original
per-diarization-turn pipeline. No ASR model runs in the API process."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from openai import NOT_GIVEN, OpenAI

from app.core.config import settings
from app.core.logging import log_operation
from app.services.transcript_align import Word

# Sent as the bearer token when a server needs no key.
_NO_KEY = "not-needed"

logger = logging.getLogger("app.services.transcription")


@dataclass
class TranscriptionResult:
    """The parsed STT output fed to alignment."""

    words: list[Word] = field(default_factory=list)
    text: str = ""
    language: str | None = None
    duration: float | None = None
    # True only when real WORD-level timestamps were returned. When False the
    # ``words`` are segment-timestamp stand-ins — too coarse to align on long
    # audio (whisper segments degrade to ~30s chunks), so the pipeline falls
    # back to per-diarization-turn transcription instead.
    has_word_timestamps: bool = False


class TranscriptionService:
    """Thin wrapper over the OpenAI-compatible transcription endpoint."""

    def __init__(self) -> None:
        self._client: OpenAI | None = None

    def _sync(self) -> OpenAI:
        if self._client is None:
            if not settings.STT_BASE_URL:
                raise ValueError("STT_BASE_URL must point to a speech-to-text endpoint")
            base_url = settings.STT_BASE_URL
            api_key = settings.STT_API_KEY or _NO_KEY
            self._client = OpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=float(settings.STT_TIMEOUT_SECONDS),
            )
        return self._client

    def transcribe_file_sync(
        self, path: str, filename: str, *, language: str | None = None
    ) -> TranscriptionResult:
        """Transcribe from a file handle — the whole recording never becomes a
        Python bytes object (issue #6). The SDK streams the handle."""
        with (
            open(path, "rb") as fh,
            log_operation(logger, "stt.transcribe", model=settings.STT_MODEL),
        ):
            response = self._sync().audio.transcriptions.create(
                model=settings.STT_MODEL,
                file=(filename, fh),
                language=language if language is not None else NOT_GIVEN,
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
            )
        return _parse_response(response)

    def transcribe_sync(
        self, audio: bytes, filename: str, *, language: str | None = None
    ) -> TranscriptionResult:
        """Transcribe audio bytes, requesting word-level timestamps."""
        buffer = io.BytesIO(audio)
        buffer.name = filename  # the SDK infers the mime type from the name
        with log_operation(
            logger, "stt.transcribe", model=settings.STT_MODEL, bytes=len(audio)
        ):
            response = self._sync().audio.transcriptions.create(
                model=settings.STT_MODEL,
                file=buffer,
                language=language if language is not None else NOT_GIVEN,
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
            )
        return _parse_response(response)


def _parse_response(response: object) -> TranscriptionResult:
    """Map an OpenAI transcription response to :class:`TranscriptionResult`.

    Handles the SDK object, a dict, and the word-vs-segment-timestamp fallback.
    """
    data = response if isinstance(response, dict) else _to_dict(response)
    text = str(data.get("text", "") or "")
    language = data.get("language")
    duration = data.get("duration")
    words: list[Word] = []
    for w in data.get("words") or []:
        wt = _get(w, "word") or _get(w, "text")
        start = _get(w, "start")
        end = _get(w, "end")
        if wt is None or start is None or end is None:
            continue
        words.append(Word(text=str(wt), start=float(start), end=float(end)))
    has_word_timestamps = bool(words)
    # Fallback: no word timestamps — use segment timestamps as coarse "words"
    # (flagged, so the worker can switch to per-turn transcription).
    if not words:
        for seg in data.get("segments") or []:
            st = _get(seg, "text")
            start = _get(seg, "start")
            end = _get(seg, "end")
            if st is None or start is None or end is None:
                continue
            words.append(Word(text=str(st), start=float(start), end=float(end)))
    return TranscriptionResult(
        words=words,
        text=text,
        language=str(language) if language else None,
        duration=float(duration) if duration is not None else None,
        has_word_timestamps=has_word_timestamps,
    )


def _to_dict(obj: object) -> dict:
    """Best-effort conversion of an SDK response to a plain dict."""
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    return {
        "text": getattr(obj, "text", ""),
        "language": getattr(obj, "language", None),
        "duration": getattr(obj, "duration", None),
        "words": getattr(obj, "words", None),
        "segments": getattr(obj, "segments", None),
    }


def _get(obj: object, key: str) -> object:
    """Read ``key`` from a dict or an attribute-style object."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


_service: TranscriptionService | None = None


def get_transcription_service() -> TranscriptionService:
    """Return the process-wide transcription service (worker)."""
    global _service
    if _service is None:
        _service = TranscriptionService()
    return _service
