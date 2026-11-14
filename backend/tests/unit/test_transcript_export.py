"""Golden-output tests for the transcript export renderers (#6, C3)."""

from __future__ import annotations

import pytest

from app.services.transcript_export import (
    EXPORT_FORMATS,
    ExportSegment,
    render,
    render_srt,
    render_vtt,
)

pytestmark = pytest.mark.unit

SEGMENTS = [
    ExportSegment(start_ms=0, end_ms=4500, speaker="Alba", text="Hello there."),
    ExportSegment(
        start_ms=3_725_250,
        end_ms=3_730_000,
        speaker="SPEAKER_01",
        text="Past the hour mark.",
    ),
]


def test_srt_golden() -> None:
    assert render_srt(SEGMENTS).decode() == (
        "1\n"
        "00:00:00,000 --> 00:00:04,500\n"
        "Alba: Hello there.\n"
        "\n"
        "2\n"
        "01:02:05,250 --> 01:02:10,000\n"
        "SPEAKER_01: Past the hour mark.\n"
    )


def test_vtt_golden() -> None:
    assert render_vtt(SEGMENTS).decode() == (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:04.500\n"
        "<v Alba>Hello there.\n"
        "\n"
        "01:02:05.250 --> 01:02:10.000\n"
        "<v SPEAKER_01>Past the hour mark.\n"
    )


def test_txt_and_md_carry_clock_stamps() -> None:
    txt = render("txt", title="Standup", segments=SEGMENTS).decode()
    assert "[0:00:00] Alba: Hello there." in txt
    assert "[1:02:05] SPEAKER_01: Past the hour mark." in txt
    md = render("md", title="Standup", segments=SEGMENTS).decode()
    assert md.startswith("# Standup")
    assert "**[0:00:00] Alba:** Hello there." in md


def test_binary_formats_produce_valid_magic_bytes() -> None:
    docx = render("docx", title="Standup", segments=SEGMENTS)
    assert docx[:2] == b"PK"  # zip container
    pdf = render("pdf", title="Standup", segments=SEGMENTS)
    assert pdf[:5] == b"%PDF-"


def test_every_advertised_format_renders() -> None:
    for fmt in EXPORT_FORMATS:
        assert render(fmt, title="T", segments=SEGMENTS)
    with pytest.raises(ValueError):
        render("odt", title="T", segments=SEGMENTS)
