"""A notebook as one Markdown file with its images, packed in a ZIP.

The archive holds ``<title>.md`` and ``images/note-001.png`` …; the document
refers to the images with relative paths, so it opens offline once extracted.
Entries are written in capture order: sessions by start time, notes by their
order within the session. A note that is still being transcribed, or failed,
keeps its image and says truthfully what state its text is in.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime

STATUS_TEXT = {
    "pending": "Transcription pending.",
    "processing": "Transcription pending.",
    "failed": "Transcription failed.",
    "no_speech": "No speech detected.",
    "screenshot_only": "Screenshot only. Tab audio was unavailable.",
}

TAG_TEXT = {
    "important": "Important",
    "unclear": "Did not understand",
    "question": "Question",
}

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True)
class ExportNote:
    session_id: str
    session_started: datetime
    seq: int
    captured_ms: int
    audio_start_ms: int | None
    audio_end_ms: int | None
    status: str
    text: str | None
    image: bytes | None
    source_time_url: str | None = None
    comment: str | None = None
    title: str | None = None
    tags: tuple[str, ...] = ()
    slide_text: str | None = None
    #: (term, definition) pairs
    terms: tuple[tuple[str, str], ...] = ()
    explanation: str | None = None
    question: str | None = None
    answer: str | None = None
    #: (title, url) of web pages the answer used
    answer_links: tuple[tuple[str, str], ...] = ()


def clock(ms: int) -> str:
    """08:42, or 1:08:42 past the hour."""
    seconds = max(0, ms) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def day(value: datetime) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def slug(title: str) -> str:
    text = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip()
    text = re.sub(r"[\s_]+", "-", text)
    return text[:80].strip("-") or "notebook"


def _alt(text: str) -> str:
    return text.replace("[", "(").replace("]", ")").replace("\n", " ")


def ordered(notes: list[ExportNote]) -> list[ExportNote]:
    return sorted(notes, key=lambda n: (n.session_started, n.session_id, n.seq))


def build_markdown(
    *, title: str, source_url: str | None, notes: list[ExportNote]
) -> tuple[str, list[tuple[str, bytes]]]:
    """The document and the image files it refers to, as (path, bytes)."""
    lines = [f"# {title}", ""]
    if source_url:
        lines += [f"Source: {source_url}", ""]
    images: list[tuple[str, bytes]] = []
    glossary: dict[str, tuple[str, str]] = {}
    current_session: str | None = None
    for number, note in enumerate(ordered(notes), start=1):
        if note.session_id != current_session:
            current_session = note.session_id
            lines += [f"## Recorded {day(note.session_started)}", ""]
        at = clock(note.captured_ms)
        if (note.title or "").strip():
            lines += [f"### {note.title.strip()}", "", f"{at} in this recording", ""]
        else:
            lines += [f"### {at} in this recording", ""]
        marks = [TAG_TEXT[tag] for tag in note.tags if tag in TAG_TEXT]
        if marks:
            lines += [f"*{' · '.join(marks)}*", ""]
        if note.image is not None:
            path = f"images/note-{number:03d}.png"
            images.append((path, note.image))
            lines += [f"![{_alt(title)}, captured at {at}]({path})", ""]
        if note.source_time_url:
            lines += [f"[Open this moment in the source]({note.source_time_url})", ""]
        if note.audio_start_ms is not None and note.audio_end_ms is not None:
            lines += [
                f"**Audio {clock(note.audio_start_ms)}–{clock(note.audio_end_ms)}**",
                "",
            ]
        if note.status == "ready" and (note.text or "").strip():
            quoted = "\n".join(f"> {row}" for row in note.text.strip().splitlines())
            lines += [quoted, ""]
        else:
            lines += [f"*{STATUS_TEXT.get(note.status, STATUS_TEXT['pending'])}*", ""]
        if (note.question or "").strip():
            lines += [f"**Question:** {note.question.strip()}", ""]
        if (note.answer or "").strip():
            lines += ["**Suggested answer**", "", note.answer.strip(), ""]
            if note.answer_links:
                lines += ["Sources:", ""]
                lines += [f"- [{_alt(t or u)}]({u})" for t, u in note.answer_links]
                lines += [""]
        if (note.explanation or "").strip():
            lines += ["**Explained simply**", "", note.explanation.strip(), ""]
        if (note.slide_text or "").strip():
            lines += ["**Text on the slide**", "", note.slide_text.strip(), ""]
        if note.terms:
            lines += ["**Key terms**", ""]
            lines += [f"- **{term}**: {definition}" for term, definition in note.terms]
            lines += [""]
            for term, definition in note.terms:
                glossary.setdefault(term.casefold(), (term, definition))
        if (note.comment or "").strip():
            # Plain paragraphs, so they cannot be mistaken for the quoted speech.
            lines += ["**My thoughts**", "", note.comment.strip(), ""]
    if glossary:
        lines += ["## Glossary", ""]
        lines += [
            f"- **{term}**: {definition}"
            for _key, (term, definition) in sorted(glossary.items())
        ]
    return "\n".join(lines).rstrip() + "\n", images


def build_zip(*, title: str, source_url: str | None, notes: list[ExportNote]) -> bytes:
    markdown, images = build_markdown(title=title, source_url=source_url, notes=notes)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{slug(title)}.md", markdown.encode("utf-8"))
        for path, data in images:
            # PNG is already compressed.
            archive.writestr(path, data, compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue()
