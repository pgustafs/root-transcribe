"""Which source a pasted link points at, so one video gets one notebook.

YouTube links come in several shapes (watch, youtu.be, shorts, embed, live,
music) and carry time, playlist and tracking parameters. All of them reduce to
``youtube:<video id>``. Any other link has no stable identity here: its
notebook is found by picking it, never by guessing from the address.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_PREFIXES = ("shorts", "embed", "live", "v")


# "mailto:x", "javascript:…": a scheme without "//". "host:8080/x" is not one.
_BARE_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")


def _with_scheme(url: str) -> str:
    text = url.strip()
    if "://" in text or _BARE_SCHEME.match(text):
        return text
    return "https://" + text


def _split(url: str):
    return urlsplit(_with_scheme(url))


def youtube_id(url: str) -> str | None:
    """The 11-character video id in a YouTube link, or None."""
    try:
        parts = _split(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    candidate: str | None = None
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = segments[0] if segments else None
    elif host in _YOUTUBE_HOSTS:
        if segments and segments[0] == "watch":
            candidate = (parse_qs(parts.query).get("v") or [None])[0]
        elif len(segments) >= 2 and segments[0] in _PATH_PREFIXES:
            candidate = segments[1]
    if candidate and _VIDEO_ID.match(candidate):
        return candidate
    return None


def source_key(url: str | None) -> str | None:
    """Stable identity for grouping, or None when the link has none."""
    if not url or not url.strip():
        return None
    video = youtube_id(url)
    return f"youtube:{video}" if video else None


def is_http_url(url: str) -> bool:
    try:
        parts = _split(url)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def normalized(url: str) -> str:
    """The link with a scheme, as stored and shown."""
    return _with_scheme(url)
