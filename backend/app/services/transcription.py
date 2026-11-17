"""OpenAI-compatible speech-to-text client for the dedicated STT_BASE_URL.

Requests word timestamps, with segment-timestamp fallback for the original
per-diarization-turn pipeline. Servers that cannot produce timestamps at all
(they reject ``verbose_json``) are asked for plain text instead, which the
per-turn pipeline also handles. No ASR model runs in the API process."""

from __future__ import annotations

import html
import io
import logging
import time
from dataclasses import dataclass, field
from typing import BinaryIO

from openai import NOT_GIVEN, APIStatusError, OpenAI

from app.core.config import settings
from app.core.logging import log_operation
from app.services.transcript_align import Word

# Sent as the bearer token when a server needs no key.
_NO_KEY = "not-needed"

logger = logging.getLogger("app.services.transcription")

# After a server rejects the timestamp request, skip it for this long so every
# later slice does not pay for a request that is known to fail.
_PLAIN_TEXT_ONLY_SECONDS = 600.0


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


class WholeFileRefused(Exception):
    """The server answered the request for a whole recording with an error.

    Servers limit what one request may hold, each in its own way (bytes,
    seconds of audio, memory), and say so only when asked. The same audio in
    short pieces is still fine, so the caller goes on with those.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class TranscriptionService:
    """Thin wrapper over the OpenAI-compatible transcription endpoint."""

    def __init__(self) -> None:
        self._client: OpenAI | None = None
        self._plain_text_until = 0.0

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

    def _request(
        self, file: BinaryIO | tuple[str, BinaryIO], language: str | None
    ) -> object:
        """Ask for timestamps; fall back to plain text when the server cannot.

        Some OpenAI-compatible servers transcribe correctly but fail any
        ``verbose_json`` request (vllm-metal answers HTTP 500). Plain ``json``
        returns the text only, which is all per-turn transcription needs.
        """
        create = self._sync().audio.transcriptions.create
        common = {
            "model": settings.STT_MODEL,
            "file": file,
            "language": language if language is not None else NOT_GIVEN,
        }
        if time.monotonic() >= self._plain_text_until:
            try:
                return create(
                    **common,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                )
            except APIStatusError as exc:
                handle = file[1] if isinstance(file, tuple) else file
                handle.seek(0)
                try:
                    response = create(**common, response_format="json")
                except APIStatusError:
                    raise exc from None  # the server is failing, not the format
                self._plain_text_until = time.monotonic() + _PLAIN_TEXT_ONLY_SECONDS
                logger.warning(
                    "STT server rejected the timestamp request; using plain text",
                    extra={
                        "operation": "stt.plain_text_fallback",
                        "status": exc.status_code,
                    },
                )
                return response
        return create(**common, response_format="json")

    def transcribe_file_sync(
        self, path: str, filename: str, *, language: str | None = None
    ) -> TranscriptionResult:
        """Transcribe from a file handle — the whole recording never becomes a
        Python bytes object (issue #6). The SDK streams the handle.

        Raises :class:`WholeFileRefused` when the server answers with an error.
        """
        try:
            with (
                open(path, "rb") as fh,
                log_operation(logger, "stt.transcribe", model=settings.STT_MODEL),
            ):
                response = self._request((filename, fh), language)
        except APIStatusError as exc:
            raise WholeFileRefused(exc.status_code, str(exc)) from exc
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
            response = self._request(buffer, language)
        return _parse_response(response)


def _parse_response(response: object) -> TranscriptionResult:
    """Map an OpenAI transcription response to :class:`TranscriptionResult`.

    Handles the SDK object, a dict, and the word-vs-segment-timestamp fallback.
    """
    data = response if isinstance(response, dict) else _to_dict(response)
    # Some models were trained on subtitle text and answer in HTML entities
    # ("Bok &amp; Demo"): the transcript is plain text.
    text = html.unescape(str(data.get("text", "") or ""))
    language = data.get("language")
    duration = data.get("duration")
    words: list[Word] = []
    for w in data.get("words") or []:
        wt = _get(w, "word") or _get(w, "text")
        start = _get(w, "start")
        end = _get(w, "end")
        if wt is None or start is None or end is None:
            continue
        words.append(
            Word(text=html.unescape(str(wt)), start=float(start), end=float(end))
        )
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
            words.append(
                Word(text=html.unescape(str(st)), start=float(start), end=float(end))
            )
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
