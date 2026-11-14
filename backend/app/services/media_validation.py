"""Typed media-validation boundary for meeting recordings (issue #37).

The transcription worker decodes an uploaded recording with ffmpeg before any
model loads. A malformed container (the 2026-07-28 incident: WebM clusters
before the EBML header) makes ffmpeg exit non-zero, and the raw
``subprocess.CalledProcessError`` — its executable path, temp file paths, and
full stderr — used to be persisted and shown to the user verbatim.

This module gives that failure a STABLE, SAFE contract:

- a small set of stable error *codes* (:class:`MediaErrorCode`) the API and UI
  branch on — never on human-readable exception strings;
- classification of an ffmpeg decode failure into one of those codes from its
  (sanitized, bounded) stderr;
- sanitization that strips commands, filesystem paths, URLs and secrets and
  enforces a length limit, leaving only the diagnostic signature operators need;
- a retryable/non-retryable split so invalid media is a terminal outcome while
  transient infrastructure failures stay retryable.

Nothing here loads audio or a model — it is the cheap authoritative boundary
that runs *before* the expensive pipeline. Callers translate a
:class:`MediaValidationError` into the persisted ``error_code`` + safe message.
"""

from __future__ import annotations

import os
import re
import subprocess
from enum import StrEnum


class MediaErrorCode(StrEnum):
    """Stable, public error categories. Values are the contract the API/UI use;
    callers must branch on these, never on human-readable messages."""

    INVALID_CONTAINER = "recording_invalid_container"
    INCOMPLETE = "recording_incomplete"
    UNSUPPORTED_CODEC = "recording_unsupported_codec"
    LIMIT_EXCEEDED = "recording_limit_exceeded"
    DECODE_TIMEOUT = "recording_decode_timeout"
    SERVICE_UNAVAILABLE = "transcription_service_unavailable"
    INTERNAL_ERROR = "transcription_internal_error"


# Retrying the SAME stored object is deterministic for these — a new/recovered
# recording is the only recovery, so the reconciler/user must not loop on them.
_NON_RETRYABLE: frozenset[MediaErrorCode] = frozenset(
    {
        MediaErrorCode.INVALID_CONTAINER,
        MediaErrorCode.INCOMPLETE,
        MediaErrorCode.UNSUPPORTED_CODEC,
        MediaErrorCode.LIMIT_EXCEEDED,
    }
)

# Safe English fallback message per code. The frontend maps the CODE to localized
# copy (en/sv); this is what non-localized surfaces (logs, older clients) show
# without leaking internals. Non-retryable messages say so explicitly.
_SAFE_MESSAGES: dict[MediaErrorCode, str] = {
    MediaErrorCode.INVALID_CONTAINER: (
        "This recording is not a valid audio file and can't be transcribed. "
        "Please make a new recording — retrying this one will not help."
    ),
    MediaErrorCode.INCOMPLETE: (
        "This recording appears incomplete or truncated and can't be "
        "transcribed. Please record it again."
    ),
    MediaErrorCode.UNSUPPORTED_CODEC: (
        "This recording uses an audio format that isn't supported. Please record "
        "again using a supported browser."
    ),
    MediaErrorCode.LIMIT_EXCEEDED: (
        "This recording exceeds the supported length or size limit."
    ),
    MediaErrorCode.DECODE_TIMEOUT: (
        "Processing this recording took too long and was stopped. You can try again."
    ),
    MediaErrorCode.SERVICE_UNAVAILABLE: (
        "The transcription service was temporarily unavailable. Please try again."
    ),
    MediaErrorCode.INTERNAL_ERROR: (
        "Transcription failed due to an unexpected internal error."
    ),
}

#: Cap on the sanitized operator diagnostic persisted/logged for a failure.
MAX_DIAGNOSTIC_CHARS = 500
#: Anything below this cannot be a valid media container (empty/truncated stub).
MIN_MEDIA_BYTES = 256


class MediaValidationError(Exception):
    """A recording failed validation/decoding with a stable, safe classification.

    ``detail`` is a SANITIZED, bounded operator diagnostic (no commands, paths,
    URLs or secrets). ``message`` is the safe user-facing English fallback.
    """

    def __init__(self, code: MediaErrorCode, *, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value)

    @property
    def retryable(self) -> bool:
        return self.code not in _NON_RETRYABLE

    @property
    def message(self) -> str:
        return _SAFE_MESSAGES[self.code]


def is_retryable(code: str | MediaErrorCode | None) -> bool:
    """Whether ``code`` (a stored ``error_code``) denotes a retryable failure.

    Unknown/legacy (``None`` or unrecognized) is treated as retryable — we only
    *suppress* retries when we positively classified a non-retryable media fault.
    """
    if code is None:
        return True
    try:
        code = MediaErrorCode(code)
    except ValueError:
        return True
    return code not in _NON_RETRYABLE


# --- sanitization ----------------------------------------------------------

# Order matters: redact URLs (may carry signed tokens) before paths.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"']+")
# A POSIX path with at least one intermediate directory segment (so a bare MIME
# like "audio/webm" is preserved but "/tmp/x/ffmpeg" or "/opt/venv/bin/x" isn't).
_ABS_PATH_RE = re.compile(r"/(?:[\w.+-]+/)+[\w.+-]*")


def sanitize_ffmpeg_stderr(stderr: str | bytes | None) -> str:
    """Redact commands, filesystem paths, URLs and secrets from ffmpeg/ffprobe
    stderr and bound its length — leaving the diagnostic signature (e.g. ``EBML
    header parsing failed``) operators need. Safe to persist and log."""
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    text = _URL_RE.sub("<url>", stderr)
    text = _WIN_PATH_RE.sub("<path>", text)
    text = _ABS_PATH_RE.sub("<path>", text)
    text = " ".join(text.split())  # collapse newlines/whitespace
    if len(text) > MAX_DIAGNOSTIC_CHARS:
        # Keep the tail — ffmpeg prints the fatal reason last.
        text = text[-MAX_DIAGNOSTIC_CHARS:]
    return text.strip()


# --- classification --------------------------------------------------------

# Checked in TIERS; within a tier the first matching signature wins.
#
# Tier 1 — DEFINITIVE container-structure failures. These outrank everything:
# the 2026-07-28 incident stderr carried both an incidental "Truncating packet"
# probe warning AND the decisive "EBML header parsing failed" — a generic
# truncation phrase must never swallow a structural header diagnosis (#37
# review round 2: the incident shape classifies as invalid_container, exactly).
_HEADER_SIGNS = (
    "ebml header",
    "header parsing failed",
    "error reading header",
    "moov atom not found",
)
_INCOMPLETE_SIGNS = (
    "truncat",
    "unexpected end",
    "premature end",
    "end of file",
    "partial file",
    "incomplete",
)
_UNSUPPORTED_SIGNS = (
    "unsupported codec",
    "codec not currently supported",
    "decoder not found",
    "not found for stream",  # "Decoder (opus) not found for stream 0"
    "no decoder",
    "unknown codec",
)
# Tier 4 — generic malformed-data fallbacks (the definitive header signatures
# live in tier-1 _HEADER_SIGNS above).
_INVALID_SIGNS = (
    "invalid data found",
    "could not find codec parameters",
    "does not contain any stream",
    "no audio",
    "invalid data",
)


def _classify_signature(detail: str) -> MediaErrorCode:
    low = detail.lower()
    if any(s in low for s in _HEADER_SIGNS):
        return MediaErrorCode.INVALID_CONTAINER
    if any(s in low for s in _INCOMPLETE_SIGNS):
        return MediaErrorCode.INCOMPLETE
    if any(s in low for s in _UNSUPPORTED_SIGNS):
        return MediaErrorCode.UNSUPPORTED_CODEC
    if any(s in low for s in _INVALID_SIGNS):
        return MediaErrorCode.INVALID_CONTAINER
    # ffmpeg rejected deterministic stored bytes with no recognized signature ⇒
    # still a media problem, and still non-retryable (the object won't change).
    return MediaErrorCode.INVALID_CONTAINER


def classify_decode_failure(exc: BaseException) -> MediaValidationError:
    """Map an ffmpeg *decode* subprocess failure to a stable media error code.

    Only call this for the decode step: an object-store/broker/STT failure is
    infrastructure (see :func:`classify_pipeline_exception`), not invalid media.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return MediaValidationError(MediaErrorCode.DECODE_TIMEOUT)
    if isinstance(exc, subprocess.CalledProcessError):
        detail = sanitize_ffmpeg_stderr(getattr(exc, "stderr", None))
        return MediaValidationError(_classify_signature(detail), detail=detail)
    return MediaValidationError(MediaErrorCode.INTERNAL_ERROR)


def classify_pipeline_exception(exc: BaseException) -> MediaValidationError:
    """Classify an *unexpected* pipeline exception (not already a
    :class:`MediaValidationError`) into a retryable infra code or an internal
    error — so the persisted/displayed message never carries ``str(exc)``.

    Kept coarse on purpose: the retry-relevant split is media (non-retryable) vs
    everything else (retryable). Known transient transports map to
    ``transcription_service_unavailable``; anything else to
    ``transcription_internal_error``.
    """
    if isinstance(exc, MediaValidationError):
        return exc
    # A subprocess timeout only ever comes from the ffmpeg *decode* (the sole
    # subprocess in the pipeline), so it IS a decode timeout.
    if isinstance(exc, subprocess.TimeoutExpired):
        return MediaValidationError(MediaErrorCode.DECODE_TIMEOUT)
    name = type(exc).__name__.lower()
    module = type(exc).__module__.lower()
    # NB: Celery's SoftTimeLimitExceeded is the WHOLE-task budget — it can fire
    # during diarize/STT/identify/persist, not just decode — so it must NOT be
    # labelled a decode timeout (issue #37 review). Treat it as a transient
    # processing/service condition instead.
    transient = (
        "softtimelimit" in name
        or module.startswith(
            ("httpx", "httpcore", "botocore", "boto3", "redis", "kombu")
        )
        or "connection" in name
        or "timeout" in name
    )
    code = (
        MediaErrorCode.SERVICE_UNAVAILABLE
        if transient
        else MediaErrorCode.INTERNAL_ERROR
    )
    return MediaValidationError(code)


# --- pre-decode structural guard ------------------------------------------


def precheck_media_file(in_path: str) -> None:
    """Cheap authoritative pre-checks before the (bounded) ffmpeg decode.

    Rejects an empty or impossibly small object up front — this is a guard, not
    the whole validation: the bounded decode + post-decode audio probe remain
    the authoritative structural check (do NOT trust filename/MIME/size alone).
    """
    size = os.path.getsize(in_path)
    if size == 0:
        raise MediaValidationError(
            MediaErrorCode.INCOMPLETE, detail="empty object (0 bytes)"
        )
    if size < MIN_MEDIA_BYTES:
        raise MediaValidationError(
            MediaErrorCode.INVALID_CONTAINER,
            detail=f"object too small to be a media container ({size} bytes)",
        )


# --- log-safe media typing --------------------------------------------------

#: Closed container tokens (mirrors ``MeetingMediaContainer`` in the schemas).
#: A stored ``content_type`` is CALLER-SUPPLIED — the upload API accepts any
#: string and a multipart part declares its own — so it must never be logged
#: verbatim: that would put free text (paths, URLs, secrets, forged log lines)
#: into the operator log through the very boundary the closed tokens exist to
#: protect (issue #37 review round 8).
_CONTAINER_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("webm", ("webm", "matroska")),
    ("ogg", ("ogg",)),
    ("mp4", ("mp4", "m4a")),
    ("mp3", ("mpeg", "mp3")),
    ("wav", ("wav",)),
    ("flac", ("flac",)),
    ("aac", ("aac",)),
)


def normalize_container(content_type: str | None) -> str:
    """Map a caller-supplied media type to a CLOSED container token.

    Anything unrecognised — including hostile input — becomes ``"unknown"``.
    """
    if not content_type:
        return "unknown"
    lowered = content_type.lower()
    for token, needles in _CONTAINER_TOKENS:
        if any(needle in lowered for needle in needles):
            return token
    return "unknown"
