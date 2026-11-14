"""Unit tests for the media-validation boundary (issue #37).

Pure logic — no ffmpeg binary, no DB. Covers the stable classification of
decode failures, ffmpeg-stderr sanitization, the retryable/non-retryable split,
and the pre-decode structural guard.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from app.services.media_validation import (
    MAX_DIAGNOSTIC_CHARS,
    MediaErrorCode,
    MediaValidationError,
    classify_decode_failure,
    classify_pipeline_exception,
    is_retryable,
    precheck_media_file,
    sanitize_ffmpeg_stderr,
)

pytestmark = pytest.mark.unit


def _called_process_error(stderr: str | bytes) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        returncode=183, cmd=["/opt/venv/bin/ffmpeg", "-i", "/tmp/x.webm"], stderr=stderr
    )


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        # The 2026-07-28 incident: WebM clusters before the EBML header.
        (b"[matroska] EBML header parsing failed\n", MediaErrorCode.INVALID_CONTAINER),
        (b"Invalid data found when processing input", MediaErrorCode.INVALID_CONTAINER),
        (b"moov atom not found", MediaErrorCode.INVALID_CONTAINER),
        (b"Truncated file, expected more data", MediaErrorCode.INCOMPLETE),
        (b"Unexpected end of file", MediaErrorCode.INCOMPLETE),
        (b"Decoder (opus) not found for stream 0", MediaErrorCode.UNSUPPORTED_CODEC),
        (b"Unsupported codec for this container", MediaErrorCode.UNSUPPORTED_CODEC),
        # ffmpeg failed with no recognized signature -> still invalid media.
        (b"conversion failed", MediaErrorCode.INVALID_CONTAINER),
        # Precedence (#37 round 2): the REAL incident stderr carries an
        # incidental "Truncating packet" probe warning AND the decisive EBML
        # header failure — the structural signature must win, never the
        # generic truncation phrase.
        (
            b"Truncating packet of size 11887 to 719\n"
            b"[matroska,webm] EBML header parsing failed\n"
            b"Invalid data found when processing input",
            MediaErrorCode.INVALID_CONTAINER,
        ),
    ],
)
def test_classify_decode_failure_maps_stderr_to_stable_code(
    stderr: bytes, expected: MediaErrorCode
) -> None:
    err = classify_decode_failure(_called_process_error(stderr))
    assert err.code is expected
    assert err.code.value == expected.value


def test_classify_decode_timeout() -> None:
    err = classify_decode_failure(
        subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=300)
    )
    assert err.code is MediaErrorCode.DECODE_TIMEOUT
    assert err.retryable is True  # a timeout under load may succeed on retry


def test_incident_fixture_is_non_retryable_invalid_container() -> None:
    """The malformed-header incident must classify deterministically and NOT be
    retried against the unchanged object."""
    err = classify_decode_failure(
        _called_process_error(b"[matroska,webm] EBML header parsing failed")
    )
    assert err.code is MediaErrorCode.INVALID_CONTAINER
    assert err.retryable is False
    assert "retrying this one will not help" in err.message


# --- sanitization ----------------------------------------------------------


def test_sanitize_strips_paths_urls_and_commands() -> None:
    raw = (
        "ffmpeg version n6.0 /opt/venv/lib/python3.12/site-packages/imageio_ffmpeg/"
        "binaries/ffmpeg-linux64 -i /tmp/tmp8xk2/recording.webm\n"
        "https://s4.local:7480/bucket/key?X-Amz-Signature=deadbeef\n"
        "[matroska] EBML header parsing failed"
    )
    clean = sanitize_ffmpeg_stderr(raw)
    assert "/tmp/" not in clean
    assert "/opt/venv" not in clean
    assert "imageio_ffmpeg" not in clean
    assert "X-Amz-Signature" not in clean
    assert "https://" not in clean
    # The useful diagnostic signature survives.
    assert "EBML header parsing failed" in clean


def test_sanitize_preserves_mime_like_tokens() -> None:
    assert "audio/webm" in sanitize_ffmpeg_stderr("guessed type audio/webm")


def test_sanitize_bounds_length() -> None:
    clean = sanitize_ffmpeg_stderr("x" * (MAX_DIAGNOSTIC_CHARS * 3))
    assert len(clean) <= MAX_DIAGNOSTIC_CHARS


def test_sanitize_none_and_bytes() -> None:
    assert sanitize_ffmpeg_stderr(None) == ""
    assert "header" in sanitize_ffmpeg_stderr(b"bad header")


# --- retryable / messages --------------------------------------------------


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        (MediaErrorCode.INVALID_CONTAINER, False),
        (MediaErrorCode.INCOMPLETE, False),
        (MediaErrorCode.UNSUPPORTED_CODEC, False),
        (MediaErrorCode.LIMIT_EXCEEDED, False),
        (MediaErrorCode.DECODE_TIMEOUT, True),
        (MediaErrorCode.SERVICE_UNAVAILABLE, True),
        (MediaErrorCode.INTERNAL_ERROR, True),
    ],
)
def test_retryable_split(code: MediaErrorCode, retryable: bool) -> None:
    assert MediaValidationError(code).retryable is retryable
    assert is_retryable(code.value) is retryable


def test_is_retryable_unknown_and_legacy_defaults_true() -> None:
    # Legacy rows (no code) and unrecognized codes are treated as retryable —
    # we only suppress retries when we positively classified a media fault.
    assert is_retryable(None) is True
    assert is_retryable("something_unknown") is True


def test_safe_message_never_empty_and_no_internal_leak() -> None:
    for code in MediaErrorCode:
        msg = MediaValidationError(code).message
        assert msg
        assert "/tmp" not in msg and "ffmpeg" not in msg.lower()


# --- pipeline exception classification -------------------------------------


def test_classify_pipeline_exception_transient_vs_internal() -> None:
    class _ConnectionResetError(Exception):
        pass

    assert (
        classify_pipeline_exception(_ConnectionResetError()).code
        is MediaErrorCode.SERVICE_UNAVAILABLE
    )
    assert (
        classify_pipeline_exception(ValueError("boom")).code
        is MediaErrorCode.INTERNAL_ERROR
    )
    # An already-typed media error passes through unchanged.
    media = MediaValidationError(MediaErrorCode.INVALID_CONTAINER)
    assert classify_pipeline_exception(media) is media


def test_soft_time_limit_is_not_a_decode_timeout() -> None:
    """A Celery whole-task soft-timeout can fire during diarize/STT/identify —
    it must NOT be mislabelled a media decode timeout (issue #37 review)."""
    from celery.exceptions import SoftTimeLimitExceeded

    err = classify_pipeline_exception(SoftTimeLimitExceeded())
    assert err.code is MediaErrorCode.SERVICE_UNAVAILABLE
    assert err.code is not MediaErrorCode.DECODE_TIMEOUT


def test_pipeline_subprocess_timeout_is_decode_timeout() -> None:
    """A subprocess timeout only comes from the ffmpeg decode, so it IS a decode
    timeout even via the general pipeline classifier."""
    err = classify_pipeline_exception(
        subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=300)
    )
    assert err.code is MediaErrorCode.DECODE_TIMEOUT


# --- pre-decode structural guard ------------------------------------------


def test_precheck_rejects_empty_and_tiny(tmp_path=None) -> None:
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        # Empty -> incomplete.
        with pytest.raises(MediaValidationError) as ei:
            precheck_media_file(path)
        assert ei.value.code is MediaErrorCode.INCOMPLETE

        # A few bytes -> invalid container.
        with open(path, "wb") as fh:
            fh.write(b"\x1a\x45\xdf\xa3")  # EBML magic but far too small
        with pytest.raises(MediaValidationError) as ei:
            precheck_media_file(path)
        assert ei.value.code is MediaErrorCode.INVALID_CONTAINER

        # A plausibly-sized object passes the cheap guard (decode is authoritative).
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 4096)
        precheck_media_file(path)  # no raise
    finally:
        os.remove(path)
