"""Unit tests for the DLP detectors and masking (issue #6, wave E4)."""

from __future__ import annotations

from app.services import dlp


def _detectors(text: str, terms=None) -> list[tuple[str, str]]:
    return [(m.detector, m.matched_text) for m in dlp.scan_text(text, terms)]


def test_email_detection() -> None:
    assert _detectors("mail me at anna.svensson+x@example.co.uk today") == [
        ("email", "anna.svensson+x@example.co.uk")
    ]


def test_card_requires_luhn() -> None:
    # 4532015112830366 passes Luhn; the same digits reordered do not.
    hits = _detectors("card 4532 0151 1283 0366 thanks")
    assert hits == [("card", "4532 0151 1283 0366")]
    assert _detectors("ref 4532015112830367") == []  # fails Luhn → not a card


def test_phone_detection_needs_enough_digits() -> None:
    hits = _detectors("call +46 70-123 45 67 tonight")
    assert hits == [("phone", "+46 70-123 45 67")]
    assert _detectors("room 12-34") == []


def test_secret_patterns() -> None:
    hits = _detectors("key AKIAIOSFODNN7EXAMPLE and api_key = supersecret99")
    assert [d for d, _ in hits] == ["secret", "secret"]


def test_card_wins_over_phone_on_same_span() -> None:
    hits = _detectors("charge 4532015112830366 now")
    assert [d for d, _ in hits] == ["card"]


def test_workspace_terms_literal_and_regex() -> None:
    terms = [
        ("Project Falcon", False, "codename"),
        (r"INV-\d{5}", True, "invoice"),
        (r"[broken", True, None),  # invalid regex must not break the scan
    ]
    hits = dlp.scan_text("project falcon ships with INV-12345 attached", terms)
    assert [(m.detector, m.matched_text, m.label) for m in hits] == [
        ("term", "project falcon", "codename"),
        ("term", "INV-12345", "invoice"),
    ]


def test_scan_returns_document_order_and_no_overlaps() -> None:
    text = "a@b.se then 4532015112830366 then c@d.se"
    hits = dlp.scan_text(text)
    starts = [m.start_char for m in hits]
    assert starts == sorted(starts)
    for i, m in enumerate(hits):
        for other in hits[i + 1 :]:
            assert m.end_char <= other.start_char or other.end_char <= m.start_char


def test_mask_spans_preserves_length_and_offsets() -> None:
    text = "call 070-1234567 now"
    masked = dlp.mask_spans(text, [(5, 16)])
    assert len(masked) == len(text)
    assert masked == "call ███████████ now"
    # Out-of-bounds spans are clamped, never raising.
    assert dlp.mask_spans("ab", [(1, 99)]) == "a█"
