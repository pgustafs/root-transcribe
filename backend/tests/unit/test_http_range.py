"""Unit matrix for the Range parser behind the playback proxy (#6, B2)."""

from __future__ import annotations

import pytest

from app.services.http_range import ByteRange, UnsatisfiableRange, parse_range

pytestmark = pytest.mark.unit

SIZE = 1000


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("bytes=0-499", ByteRange(0, 499)),
        ("bytes=500-", ByteRange(500, 999)),
        ("bytes=-200", ByteRange(800, 999)),
        # Over-long end clamps to EOF.
        ("bytes=900-5000", ByteRange(900, 999)),
        # Suffix longer than the object serves the whole object as 206.
        ("bytes=-5000", ByteRange(0, 999)),
        # Multi-range and malformed headers fall back to a full 200.
        ("bytes=0-1,5-9", None),
        ("bytes=abc-def", None),
        ("chunks=0-1", None),
        ("bytes=--", None),
        ("bytes=9-5", None),
    ],
)
def test_parse_range_matrix(header: str | None, expected: ByteRange | None) -> None:
    assert parse_range(header, SIZE) == expected


def test_unsatisfiable_ranges_are_416() -> None:
    with pytest.raises(UnsatisfiableRange) as exc:
        parse_range("bytes=1000-", SIZE)  # starts at EOF
    assert str(exc.value) == "bytes */1000"
    with pytest.raises(UnsatisfiableRange):
        parse_range("bytes=-0", SIZE)  # zero-length suffix
    with pytest.raises(UnsatisfiableRange):
        parse_range("bytes=0-", 0)  # empty object


def test_byte_range_headers() -> None:
    br = ByteRange(200, 499)
    assert br.length == 300
    assert br.content_range(1000) == "bytes 200-499/1000"
