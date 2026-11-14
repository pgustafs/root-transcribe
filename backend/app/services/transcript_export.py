"""Transcript export renderers (issue #6, wave C3).

One revision's segments → Markdown, plain text, SRT, WebVTT, DOCX, or PDF.
The subtitle formats (SRT/VTT) carry the exact segment time ranges, so an
exported transcript stays linkable back to the audio. DOCX uses ``python-docx``
and PDF uses ``reportlab`` — both pure-Python, so the API image needs no
system dependencies. Everything here is deterministic and side-effect free.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

EXPORT_FORMATS = ("md", "txt", "srt", "vtt", "docx", "pdf")

CONTENT_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pdf": "application/pdf",
}


@dataclass(frozen=True)
class ExportSegment:
    """The slice of a transcript segment the renderers need."""

    start_ms: int
    end_ms: int
    speaker: str
    text: str


def _ts(ms: int, *, sep: str) -> str:
    """``HH:MM:SS<sep>mmm`` — SRT uses a comma, VTT a period."""
    hours, rest = divmod(max(0, ms), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def _clock(ms: int) -> str:
    """``[H:MM:SS]`` display stamp for text-ish formats."""
    total = max(0, ms) // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def render_txt(title: str, segments: list[ExportSegment]) -> bytes:
    lines = [title, "=" * len(title), ""]
    for seg in segments:
        lines.append(f"[{_clock(seg.start_ms)}] {seg.speaker}: {seg.text}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_md(title: str, segments: list[ExportSegment]) -> bytes:
    lines = [f"# {title}", ""]
    for seg in segments:
        lines.append(f"**[{_clock(seg.start_ms)}] {seg.speaker}:** {seg.text}")
        lines.append("")
    return ("\n".join(lines)).encode("utf-8")


def render_srt(segments: list[ExportSegment]) -> bytes:
    blocks = []
    for index, seg in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n"
            f"{_ts(seg.start_ms, sep=',')} --> {_ts(seg.end_ms, sep=',')}\n"
            f"{seg.speaker}: {seg.text}\n"
        )
    return ("\n".join(blocks)).encode("utf-8")


def render_vtt(segments: list[ExportSegment]) -> bytes:
    blocks = ["WEBVTT\n"]
    for seg in segments:
        blocks.append(
            f"{_ts(seg.start_ms, sep='.')} --> {_ts(seg.end_ms, sep='.')}\n"
            f"<v {seg.speaker}>{seg.text}\n"
        )
    return ("\n".join(blocks)).encode("utf-8")


def render_docx(
    title: str,
    segments: list[ExportSegment],
    appendix: list[str] | None = None,
) -> bytes:
    from docx import Document

    document = Document()
    document.add_heading(title, level=1)
    for seg in segments:
        paragraph = document.add_paragraph()
        run = paragraph.add_run(f"[{_clock(seg.start_ms)}] {seg.speaker}: ")
        run.bold = True
        paragraph.add_run(seg.text)
    # Provenance appendix (issue #14): binary formats can't be concatenated
    # after rendering the way md/txt are, so the caller's lines are written
    # INTO the document — otherwise docx/pdf silently lose the source
    # inventory and coverage the docs promise.
    for line in appendix or []:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render_pdf(
    title: str,
    segments: list[ExportSegment],
    appendix: list[str] | None = None,
) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph(_escape(title), styles["Title"]), Spacer(1, 12)]
    for seg in segments:
        story.append(
            Paragraph(
                f"<b>[{_clock(seg.start_ms)}] {_escape(seg.speaker)}:</b> "
                f"{_escape(seg.text)}",
                styles["BodyText"],
            )
        )
    # See render_docx: the appendix is rendered in, not appended after.
    if appendix:
        story.append(Spacer(1, 12))
        for line in appendix:
            story.append(Paragraph(_escape(line), styles["BodyText"]))
    doc.build(story)
    return buffer.getvalue()


def render(
    fmt: str,
    *,
    title: str,
    segments: list[ExportSegment],
    appendix: list[str] | None = None,
) -> bytes:
    """Dispatch to one renderer; ``fmt`` must be in :data:`EXPORT_FORMATS`.

    ``appendix`` (provenance lines) is written into the binary document
    formats; text formats let the caller concatenate it instead, and caption
    formats never carry it.
    """
    if fmt == "md":
        return render_md(title, segments)
    if fmt == "txt":
        return render_txt(title, segments)
    if fmt == "srt":
        return render_srt(segments)
    if fmt == "vtt":
        return render_vtt(segments)
    if fmt == "docx":
        return render_docx(title, segments, appendix)
    if fmt == "pdf":
        return render_pdf(title, segments, appendix)
    raise ValueError(f"unsupported export format: {fmt}")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
