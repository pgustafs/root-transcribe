"""DLP detection for meeting transcripts (issue #6, wave E4).

Pure regex/checksum detectors over segment text — no model calls, so a scan is
cheap enough to run on every completed transcription. Built-ins cover the
classic exfiltration classes (email, phone, payment card via Luhn, credential
material); workspaces add their own terms (literal or regex). Detection only
*proposes*: a human accepts findings before anything is redacted, and
redaction itself is a new superseding revision + muted audio rendition — the
original text is never edited in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ordered: the first detector to claim a span wins (card before phone — a card
# number is also a plausible phone match).
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
_CARD = re.compile(r"(?<!\d)\d(?:[ \-]?\d){12,18}(?!\d)")
_PHONE = re.compile(r"(?<![\w/])\+?\d[\d \-()]{6,16}\d(?![\w/])")
_SECRET = re.compile(
    r"AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|ghp_[A-Za-z0-9]{36}"  # GitHub PAT
    r"|sk-[A-Za-z0-9\-_]{20,}"  # API secret key style
    r"|(?i:(?:api[_ -]?key|secret|token|password))\s*[:=]\s*\S{8,}",
)

#: The character redacted text is masked with.
MASK_CHAR = "█"


@dataclass(frozen=True)
class DlpMatch:
    """One detector hit inside one piece of text."""

    detector: str  # email | phone | card | secret | term
    matched_text: str
    start_char: int
    end_char: int
    label: str | None = None  # the workspace term's label, if any


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scan_text(
    text: str, terms: list[tuple[str, bool, str | None]] | None = None
) -> list[DlpMatch]:
    """All detector hits in ``text``, non-overlapping, in document order.

    ``terms`` are workspace additions: ``(pattern, is_regex, label)`` —
    literals match case-insensitively; an invalid regex term is skipped rather
    than failing the scan.
    """
    matches: list[DlpMatch] = []
    claimed: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in claimed)

    def claim(match: DlpMatch) -> None:
        matches.append(match)
        claimed.append((match.start_char, match.end_char))

    for m in _EMAIL.finditer(text):
        claim(DlpMatch("email", m.group(), m.start(), m.end()))
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits) and free(m.start(), m.end()):
            claim(DlpMatch("card", m.group(), m.start(), m.end()))
    for m in _SECRET.finditer(text):
        if free(m.start(), m.end()):
            claim(DlpMatch("secret", m.group(), m.start(), m.end()))
    for m in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        # E.164 bounds: an 8–15 digit run; longer runs are ids/cards, not phones.
        if 8 <= len(digits) <= 15 and free(m.start(), m.end()):
            claim(DlpMatch("phone", m.group(), m.start(), m.end()))
    for pattern, is_regex, label in terms or []:
        try:
            rx = (
                re.compile(pattern)
                if is_regex
                else re.compile(re.escape(pattern), re.IGNORECASE)
            )
        except re.error:
            continue  # a bad workspace regex must not break the scan
        for m in rx.finditer(text):
            if m.start() == m.end():
                continue
            if free(m.start(), m.end()):
                claim(DlpMatch("term", m.group(), m.start(), m.end(), label))

    return sorted(matches, key=lambda match: match.start_char)


def mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace each span with mask characters of the same length (offsets of
    everything outside the spans are preserved for anchoring)."""
    chars = list(text)
    for start, end in spans:
        for i in range(max(0, start), min(len(chars), end)):
            chars[i] = MASK_CHAR
    return "".join(chars)
