"""Export a Markdown note as Markdown, Word or PDF.

Word and PDF are produced with the libraries already used for transcripts
(``python-docx``, ``reportlab``). Markdown is parsed with ``markdown-it-py``
into a small list of blocks that both renderers understand: headings,
paragraphs, bullet / numbered / check lists, quotes, code, tables and rules.
Anything else degrades to plain text rather than failing the export.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

EXPORT_FORMATS = ("md", "docx", "pdf")

CONTENT_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


@dataclass(frozen=True)
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass
class Block:
    # heading | paragraph | item | quote | code | table | rule
    kind: str
    runs: list[Run] = field(default_factory=list)
    level: int = 0  # heading level, or list nesting depth (0 = top)
    number: int | None = None  # numbered-list position
    checked: bool | None = None  # check-list state
    text: str = ""  # code
    rows: list[list[list[Run]]] = field(default_factory=list)  # table


def _inline(token) -> list[Run]:
    runs: list[Run] = []
    bold = italic = 0
    link: str | None = None
    for child in token.children or []:
        kind = child.type
        if kind == "text":
            runs.append(Run(child.content, bold > 0, italic > 0))
        elif kind == "code_inline":
            runs.append(Run(child.content, bold > 0, italic > 0, code=True))
        elif kind in ("softbreak", "hardbreak"):
            runs.append(Run(" " if kind == "softbreak" else "\n", bold > 0, italic > 0))
        elif kind == "strong_open":
            bold += 1
        elif kind == "strong_close":
            bold -= 1
        elif kind == "em_open":
            italic += 1
        elif kind == "em_close":
            italic -= 1
        elif kind == "link_open":
            link = child.attrGet("href")
        elif kind == "link_close":
            if link and not any(link in run.text for run in runs[-1:]):
                runs.append(Run(f" ({link})", bold > 0, italic > 0))
            link = None
        elif child.content:
            runs.append(Run(child.content, bold > 0, italic > 0))
    return runs


def _take_checkbox(runs: list[Run]) -> tuple[bool | None, list[Run]]:
    """``[ ] text`` / ``[x] text`` at the start of a list item."""
    if not runs or runs[0].code:
        return None, runs
    head = runs[0].text
    for mark, state in (("[ ] ", False), ("[x] ", True), ("[X] ", True)):
        if head.startswith(mark):
            rest = Run(head[len(mark) :], runs[0].bold, runs[0].italic)
            return state, ([rest] if rest.text else []) + runs[1:]
    return None, runs


def parse(markdown: str) -> list[Block]:
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").enable("table").parse(markdown or "")
    blocks: list[Block] = []
    lists: list[list[int | None]] = []  # per open list: [next number or None]
    item_started = False  # the next paragraph is the item's first line
    quote = 0
    row: list[list[Run]] | None = None
    table: Block | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "heading_open":
            inline = tokens[index + 1]
            blocks.append(Block("heading", _inline(inline), level=int(token.tag[1])))
            index += 2
        elif kind in ("bullet_list_open", "ordered_list_open"):
            start = int(token.attrGet("start") or 1)
            lists.append([start if kind == "ordered_list_open" else None])
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            item_started = True
        elif kind == "blockquote_open":
            quote += 1
        elif kind == "blockquote_close":
            quote -= 1
        elif kind == "paragraph_open":
            runs = _inline(tokens[index + 1])
            if lists and item_started:
                checked, runs = _take_checkbox(runs)
                number = lists[-1][0]
                if number is not None:
                    lists[-1][0] = number + 1
                blocks.append(
                    Block(
                        "item",
                        runs,
                        level=len(lists) - 1,
                        number=number,
                        checked=checked,
                    )
                )
                item_started = False
            else:
                blocks.append(
                    Block("quote" if quote else "paragraph", runs, level=len(lists))
                )
            index += 2
        elif kind in ("fence", "code_block"):
            blocks.append(Block("code", text=token.content.rstrip("\n")))
        elif kind == "hr":
            blocks.append(Block("rule"))
        elif kind == "table_open":
            table = Block("table")
        elif kind == "tr_open":
            row = []
        elif kind in ("th_open", "td_open"):
            if row is not None:
                row.append(_inline(tokens[index + 1]))
        elif kind == "tr_close":
            if table is not None and row is not None:
                table.rows.append(row)
            row = None
        elif kind == "table_close":
            if table is not None and table.rows:
                blocks.append(table)
            table = None
        elif kind == "html_block" and token.content.strip():
            blocks.append(Block("paragraph", [Run(token.content.strip())]))
        index += 1
    return blocks


def _box(checked: bool | None, *, ascii_only: bool) -> str:
    if checked is None:
        return ""
    if ascii_only:
        return "[x] " if checked else "[ ] "
    return "☑ " if checked else "☐ "


def render_docx(title: str, markdown: str) -> bytes:
    from docx import Document
    from docx.shared import Cm, Pt

    document = Document()
    document.core_properties.title = title

    def add_runs(paragraph, runs: list[Run]) -> None:
        for run in runs:
            piece = paragraph.add_run(run.text)
            piece.bold = run.bold or None
            piece.italic = run.italic or None
            if run.code:
                piece.font.name = "Courier New"

    for block in parse(markdown):
        if block.kind == "heading":
            add_runs(document.add_heading(level=min(block.level, 4)), block.runs)
        elif block.kind == "item":
            style = "List Number" if block.number is not None else "List Bullet"
            if block.checked is not None:
                style = "Normal"
            paragraph = document.add_paragraph(style=style)
            paragraph.paragraph_format.left_indent = Cm(0.63 + 0.63 * block.level)
            box = _box(block.checked, ascii_only=False)
            add_runs(paragraph, ([Run(box)] if box else []) + block.runs)
        elif block.kind in ("paragraph", "quote"):
            paragraph = document.add_paragraph()
            if block.kind == "quote" or block.level:
                paragraph.paragraph_format.left_indent = Cm(0.63 * max(block.level, 1))
            add_runs(paragraph, block.runs)
            if block.kind == "quote":
                for piece in paragraph.runs:
                    piece.italic = True
        elif block.kind == "code":
            piece = document.add_paragraph().add_run(block.text)
            piece.font.name = "Courier New"
            piece.font.size = Pt(9)
        elif block.kind == "table":
            columns = max(len(row) for row in block.rows)
            grid = document.add_table(rows=len(block.rows), cols=columns)
            grid.style = "Table Grid"
            for r, row in enumerate(block.rows):
                for c, cell in enumerate(row):
                    paragraph = grid.cell(r, c).paragraphs[0]
                    add_runs(paragraph, cell)
                    if r == 0:
                        for piece in paragraph.runs:
                            piece.bold = True
        elif block.kind == "rule":
            document.add_paragraph("—" * 12)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _markup(runs: list[Run]) -> str:
    """reportlab's mini-HTML for a line of runs."""
    out = []
    for run in runs:
        text = _escape(run.text).replace("\n", "<br/>")
        if run.code:
            text = f'<font face="Courier">{text}</font>'
        if run.italic:
            text = f"<i>{text}</i>"
        if run.bold:
            text = f"<b>{text}</b>"
        out.append(text)
    return "".join(out)


def render_pdf(title: str, markdown: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

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
    body = styles["BodyText"]
    headings = {1: "Heading1", 2: "Heading2", 3: "Heading3"}
    story: list = []
    for block in parse(markdown):
        if block.kind == "heading":
            style = styles[headings.get(block.level, "Heading4")]
            story.append(Paragraph(_markup(block.runs), style))
        elif block.kind == "item":
            indent = 14 + 14 * block.level
            if block.checked is not None:
                bullet = _box(block.checked, ascii_only=True).strip()
            elif block.number is not None:
                bullet = f"{block.number}."
            else:
                bullet = "•"
            width = 16 if len(bullet) > 1 else 0
            style = ParagraphStyle(
                "item",
                parent=body,
                leftIndent=indent + width,
                bulletIndent=indent - 10,
            )
            story.append(Paragraph(_markup(block.runs), style, bulletText=bullet))
        elif block.kind in ("paragraph", "quote"):
            indent = 14 * max(block.level, 1 if block.kind == "quote" else 0)
            style = ParagraphStyle("p", parent=body, leftIndent=indent)
            text = _markup(block.runs)
            story.append(
                Paragraph(f"<i>{text}</i>" if block.kind == "quote" else text, style)
            )
        elif block.kind == "code":
            story.append(Preformatted(block.text, styles["Code"]))
            story.append(Spacer(1, 6))
        elif block.kind == "table":
            columns = max(len(row) for row in block.rows)
            data = [
                [
                    Paragraph(
                        f"<b>{_markup(cell)}</b>" if r == 0 else _markup(cell), body
                    )
                    for cell in row + [[]] * (columns - len(row))
                ]
                for r, row in enumerate(block.rows)
            ]
            grid = Table(data, repeatRows=1)
            grid.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.extend([grid, Spacer(1, 8)])
        elif block.kind == "rule":
            story.append(HRFlowable(width="100%", color=colors.grey))
    if not story:
        story.append(Paragraph(_escape(title), styles["Title"]))
    doc.build(story)
    return buffer.getvalue()


def render(fmt: str, *, title: str, markdown: str) -> bytes:
    """Dispatch to one renderer; ``fmt`` must be in :data:`EXPORT_FORMATS`."""
    if fmt == "md":
        return (markdown.rstrip("\n") + "\n").encode("utf-8")
    if fmt == "docx":
        return render_docx(title, markdown)
    if fmt == "pdf":
        return render_pdf(title, markdown)
    raise ValueError(f"unsupported export format: {fmt}")
