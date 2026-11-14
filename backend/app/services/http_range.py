"""HTTP Range header parsing for the audio playback proxy (issue #6, wave B2).

Pure logic (RFC 9110 §14) so the 206/416 semantics unit-test exactly:

- single ranges (``bytes=a-b``, ``bytes=a-``, ``bytes=-n``) are satisfied;
- multiple ranges fall back to a full 200 (allowed by the RFC — one body,
  no multipart/byteranges complexity);
- syntactically invalid ``Range`` headers are ignored (full 200, per RFC);
- an unsatisfiable range (start beyond EOF, or a zero-length suffix) is a 416.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass(frozen=True)
class ByteRange:
    """A satisfiable byte range: inclusive start/end offsets."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range(self, size: int) -> str:
        return f"bytes {self.start}-{self.end}/{size}"


class UnsatisfiableRange(Exception):
    """The Range was well-formed but cannot be satisfied → 416."""


def parse_range(header: str | None, size: int) -> ByteRange | None:
    """Resolve a ``Range`` header against an object of ``size`` bytes.

    Returns None for "serve the full object" (no header, multi-range, or a
    malformed header — the RFC says to ignore those); raises
    :class:`UnsatisfiableRange` for a well-formed range outside the object.
    """
    if not header or size <= 0:
        if header and size <= 0:
            raise UnsatisfiableRange(f"bytes */{size}")
        return None
    value = header.strip()
    if "," in value:
        return None  # multiple ranges → full 200 (RFC-permitted fallback)
    match = _RANGE_RE.match(value)
    if match is None:
        return None  # malformed → ignore per RFC 9110
    first, last = match.group(1), match.group(2)
    if first == "" and last == "":
        return None
    if first == "":
        # Suffix form: last N bytes.
        suffix = int(last)
        if suffix == 0:
            raise UnsatisfiableRange(f"bytes */{size}")
        start = max(0, size - suffix)
        return ByteRange(start=start, end=size - 1)
    start = int(first)
    if start >= size:
        raise UnsatisfiableRange(f"bytes */{size}")
    end = min(int(last), size - 1) if last != "" else size - 1
    if end < start:
        return None  # malformed (end before start) → ignore
    return ByteRange(start=start, end=end)
