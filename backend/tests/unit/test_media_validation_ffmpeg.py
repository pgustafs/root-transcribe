"""Real-ffmpeg media-validation tests (issue #37).

These decode through the SAME imageio-ffmpeg binary the stt image ships, rather
than mocking the decoder boundary. They cover the two things a mock cannot:

1. a genuinely malformed container is rejected by the real ffmpeg and classified
   into a stable media code — including the EXACT 2026-07-28 incident shape
   (cluster data before a late EBML header); and
2. the operator log carries NO raw ffmpeg command / executable / temp paths — the
   leak (issue #37 finding 2) that a mocked decoder test could not detect.

``imageio_ffmpeg`` is an ``[stt]``/``[ffmpeg]`` extra, absent from the base/dev
test env, so by default this file skips. The dedicated ``media:ffmpeg`` CI job
installs the lightweight ``ffmpeg`` extra and sets ``REQUIRE_FFMPEG=1``, which
turns EVERY skip in this file into a hard failure — the regression is
CI-enforced, never silently skipped (issue #37 review round 2).
"""

from __future__ import annotations

import logging
import os
import subprocess

import pytest

REQUIRE_FFMPEG = os.environ.get("REQUIRE_FFMPEG") == "1"

if REQUIRE_FFMPEG:
    # The enforcing CI job: a missing dependency is a broken job, not a skip.
    import imageio_ffmpeg
else:
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")

from app.services.diarization import decode_file_to_wav  # noqa: E402
from app.services.media_validation import (  # noqa: E402
    MediaErrorCode,
    MediaValidationError,
)

pytestmark = pytest.mark.unit


def _skip_or_fail(reason: str) -> None:
    """Skip locally; FAIL under REQUIRE_FFMPEG (CI must actually run these)."""
    if REQUIRE_FFMPEG:
        pytest.fail(f"REQUIRE_FFMPEG=1 but: {reason}")
    pytest.skip(reason)


def _ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _make_valid_webm(path: str) -> None:
    """0.2 s of silence encoded to webm/opus by the shipped ffmpeg."""
    proc = subprocess.run(
        [
            _ffmpeg(), "-y", "-f", "lavfi",
            "-i", "anullsrc=r=16000:cl=mono", "-t", "0.2",
            "-c:a", "libopus", path,
        ],
        capture_output=True,
    )  # fmt: skip
    if proc.returncode != 0 or not os.path.getsize(path):
        _skip_or_fail("bundled ffmpeg cannot encode webm/opus")


def test_valid_webm_decodes_through_shipped_ffmpeg(tmp_path) -> None:
    src = tmp_path / "good.webm"
    _make_valid_webm(str(src))
    out = decode_file_to_wav(str(src), src_hint="good.webm", max_seconds=60)
    try:
        assert os.path.getsize(out) > 0
    finally:
        if os.path.exists(out):
            os.remove(out)


def test_clusters_before_late_ebml_header_is_invalid_container(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The EXACT 2026-07-28 incident shape (issue #37): cluster/garbage bytes
    at the START of the file with the EBML magic only appearing LATER. The
    real ffmpeg must reject it, and classification must be precisely
    ``recording_invalid_container`` — not merely "some media error"."""
    bad = tmp_path / "incident.webm"
    # > MIN_MEDIA_BYTES so the pre-check passes and the DECODE is what fails.
    # Cluster-ish payload first; the EBML magic (\x1a\x45\xdf\xa3) only later —
    # a recorder that flushed clusters before the header, byte order as seen
    # in the incident.
    bad.write_bytes(
        b"\x1f\x43\xb6\x75"
        + b"not a real webm cluster " * 24  # cluster id + noise
        + b"\x1a\x45\xdf\xa3"
        + b"\x9f\x42\x86\x81\x01"  # EBML header, too late
        + b"trailing payload " * 8
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(MediaValidationError) as ei,
    ):
        decode_file_to_wav(str(bad), src_hint="incident.webm", max_seconds=60)

    # The incident regression: EXACTLY the invalid-container code.
    assert ei.value.code is MediaErrorCode.INVALID_CONTAINER
    _assert_no_leaks(caplog.text, str(bad))


def test_garbage_after_valid_magic_is_classified_without_leaking_paths(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A second malformed shape (valid EBML magic, then garbage): the real
    ffmpeg rejects it into SOME stable media code — generic malformed-media
    handling; the incident-exact case above pins the strict classification."""
    bad = tmp_path / "bad.webm"
    bad.write_bytes(b"\x1a\x45\xdf\xa3" + b"not a real webm cluster " * 32)

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(MediaValidationError) as ei,
    ):
        decode_file_to_wav(str(bad), src_hint="bad.webm", max_seconds=60)

    assert ei.value.code in (
        MediaErrorCode.INVALID_CONTAINER,
        MediaErrorCode.INCOMPLETE,
        MediaErrorCode.UNSUPPORTED_CODEC,
    )
    _assert_no_leaks(caplog.text, str(bad))


def _assert_no_leaks(blob: str, input_path: str) -> None:
    # The F2 regression: nothing in the operator log may carry the ffmpeg
    # executable path or the input temp path (caplog.text includes formatted
    # tracebacks).
    assert input_path not in blob
    assert _ffmpeg() not in blob
    assert "/tmp/" not in blob
