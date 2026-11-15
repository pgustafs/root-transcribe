"""A question heard in a meeting: finding it, and suggesting an answer.

A note marked "question" holds up to a minute of what was said. Three steps:

1. The language model picks the question out of the talk and gives a first,
   short answer from what it knows (shown at once).
2. The workspace is searched: transcripts of earlier recordings and the
   learner's own notes. Only material the learner may read is used, and
   recordings marked restricted are left out.
3. If the session allows it, an agent researches the question on the web.
   Only the question is given to that agent, never the transcript.

The final answer is written from those findings and names its sources. It is a
suggestion: the sources are stored as data, not taken from the model's text.

Pure logic and database reads; the model calls go through the agent runner.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.meeting_permissions import (
    MeetingCapability,
    apply_grants,
    base_capabilities,
)
from app.models.meeting_governance import MeetingPermissionGrant
from app.models.meeting_recording import MeetingRecording
from app.models.meeting_transcript_segment import MeetingTranscriptSegment
from app.models.study import StudyNote, StudyNotebook
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services.study_enrich import _json_object
from app.services.workspace_access import effective_role

EXTRACT_INSTRUCTIONS = """\
You get the last minute of a meeting or lecture as a speech-to-text transcript
(it may have errors), and sometimes the text of the slide that was showing.
Somebody just asked a question. Find it.

Answer with JSON only, no other text, in this form:
{"question": "...", "search_terms": ["...", "..."], "answer": "..."}

- "question": the most recent question that was asked, written as one clear,
  complete question that can be understood without the transcript (fill in
  what "it" or "that" refers to). Empty string if nobody asked a question.
- "search_terms": 3 to 6 distinctive words or short names from the question
  that would find related material: nouns, product names, technical terms.
  No filler words.
- "answer": a first answer in at most three sentences, only if you are
  confident from general knowledge. If the question is about this group's own
  matters (their decisions, plans, systems, people) or you are not sure, an
  empty string.

Write in the language the question was asked in.
"""

COMPOSE_INSTRUCTIONS = """\
You help someone answer a question that was asked in a meeting. You get the
question and findings: numbered excerpts from the person's own earlier
recordings and notes (marked [S1], [S2] ...), and sometimes the result of web
research.

Answer with JSON only, no other text, in this form:
{"short_answer": "...", "answer": "..."}

- "short_answer": the answer in at most three sentences, ready to be said
  aloud. If the findings do not answer the question, say so plainly in one
  sentence and give the closest thing that is known.
- "answer": the full answer in Markdown, at most 250 words. After each
  statement taken from an excerpt, put its mark, like [S2]. Statements from
  the web research keep the links they came with. Do not state anything that
  is not supported by the findings, except generally known facts, and say when
  the findings disagree.

Write in the language of the question.
"""

MAX_QUESTION = 500
MAX_SHORT = 700
MAX_ANSWER = 6000
MAX_TERMS = 6
MAX_FINDINGS = 6
#: Rows looked at before ranking.
CANDIDATES = 300
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "do",
        "does",
        "did",
        "what",
        "which",
        "who",
        "how",
        "why",
        "when",
        "where",
        "that",
        "this",
        "it",
        "we",
        "you",
        "they",
        "our",
        "your",
        "can",
        "could",
        "should",
        "would",
        "will",
        "about",
        "from",
        "at",
        "by",
        "as",
        "not",
        "if",
        "then",
        "than",
        "there",
        "their",
        "have",
        "has",
        "had",
        "vad",
        "hur",
        "varför",
        "när",
        "var",
        "vilken",
        "vilket",
        "vilka",
        "som",
        "att",
        "och",
        "eller",
        "en",
        "ett",
        "den",
        "det",
        "de",
        "vi",
        "ni",
        "du",
        "jag",
        "är",
        "var",
        "har",
        "hade",
        "kan",
        "ska",
        "skulle",
        "om",
        "på",
        "i",
        "av",
        "för",
        "med",
        "till",
        "från",
        "inte",
    ]
)


@dataclass(frozen=True)
class Extracted:
    question: str | None
    terms: list[str]
    answer: str | None


@dataclass(frozen=True)
class Finding:
    """One excerpt from the workspace."""

    kind: str  # recording | note
    title: str
    text: str
    recording_id: uuid.UUID | None = None
    start_ms: int | None = None
    notebook_id: uuid.UUID | None = None
    note_id: uuid.UUID | None = None

    def source(self, label: str) -> dict:
        data: dict = {"kind": self.kind, "label": label, "title": self.title}
        if self.recording_id is not None:
            data["recording_id"] = str(self.recording_id)
            data["start_ms"] = self.start_ms or 0
        if self.notebook_id is not None:
            data["notebook_id"] = str(self.notebook_id)
            data["note_id"] = str(self.note_id)
        return data


def clean_terms(values: object) -> list[str]:
    """Search words worth looking for: no filler, no duplicates."""
    terms: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else []:
        term = " ".join(str(value).split()).strip(" .,;:?!\"'")
        key = term.casefold()
        if len(term) < 3 or len(term) > 60 or key in _STOP or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) == MAX_TERMS:
            break
    return terms


def terms_from_question(question: str) -> list[str]:
    """A fallback when the model gave no search words."""
    words = re.findall(r"[\w-]{4,}", question, flags=re.UNICODE)
    return clean_terms(sorted(set(words), key=lambda word: -len(word)))


def parse_extract(answer: str) -> Extracted:
    data = _json_object(answer)
    question = " ".join(str(data.get("question") or "").split())[:MAX_QUESTION]
    first = str(data.get("answer") or "").strip()[:MAX_SHORT]
    terms = clean_terms(data.get("search_terms"))
    if question and not terms:
        terms = terms_from_question(question)
    return Extracted(question=question or None, terms=terms, answer=first or None)


def build_extract_input(*, text: str, slide_text: str | None) -> str:
    parts = ["Transcript:\n" + text.strip()]
    if (slide_text or "").strip():
        parts.append("Text on the slide:\n" + slide_text.strip()[:4000])
    return "\n\n".join(parts)


def can_search_workspace(
    session: Session, *, user: User, workspace_id: uuid.UUID
) -> bool:
    """May this user read transcripts in the workspace (role and grants)?"""
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        return False
    membership = session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    role = effective_role(user=user, workspace=workspace, membership=membership)
    grants = (
        session.execute(
            select(MeetingPermissionGrant)
            .where(MeetingPermissionGrant.workspace_id == workspace_id)
            .order_by(MeetingPermissionGrant.created_at)
        )
        .scalars()
        .all()
    )
    capabilities = apply_grants(
        base_capabilities(user=user, effective_role=role),
        list(grants),
        user_id=user.id,
        effective_role=role,
    )
    return MeetingCapability.VIEW_TRANSCRIPT in capabilities


def _score(text: str, terms: list[str]) -> int:
    lowered = text.casefold()
    return sum(1 for term in terms if term.casefold() in lowered)


def _matches(column, terms: list[str]):
    return or_(*[func.lower(column).like(f"%{term.lower()}%") for term in terms])


def search_workspace(
    session: Session,
    *,
    user: User,
    workspace_id: uuid.UUID,
    terms: list[str],
    exclude_note_id: uuid.UUID | None = None,
    exclude_recording_id: uuid.UUID | None = None,
) -> list[Finding]:
    """The best excerpts for the search words, from earlier recordings the
    user may read and from the user's own notes. Ranked by how many different
    words an excerpt contains."""
    if not terms:
        return []
    ranked: list[tuple[int, int, Finding]] = []
    if can_search_workspace(session, user=user, workspace_id=workspace_id):
        shown = func.coalesce(
            MeetingRecording.approved_revision_id, MeetingRecording.latest_revision_id
        )
        statement = (
            select(MeetingTranscriptSegment, MeetingRecording)
            .join(
                MeetingRecording,
                MeetingRecording.id == MeetingTranscriptSegment.recording_id,
            )
            .where(
                MeetingRecording.workspace_id == workspace_id,
                # Restricted recordings need an explicit grant per use.
                MeetingRecording.sensitivity != "restricted",
                MeetingTranscriptSegment.revision_id == shown,
                _matches(MeetingTranscriptSegment.text, terms),
            )
            .order_by(MeetingRecording.created_at.desc(), MeetingTranscriptSegment.idx)
            .limit(CANDIDATES)
        )
        if exclude_recording_id is not None:
            statement = statement.where(MeetingRecording.id != exclude_recording_id)
        for order, (segment, recording) in enumerate(session.execute(statement).all()):
            around = (
                session.execute(
                    select(MeetingTranscriptSegment)
                    .where(
                        MeetingTranscriptSegment.revision_id == segment.revision_id,
                        MeetingTranscriptSegment.idx.between(
                            segment.idx - 1, segment.idx + 1
                        ),
                    )
                    .order_by(MeetingTranscriptSegment.idx)
                )
                .scalars()
                .all()
                if order < 40
                else [segment]
            )
            text = " ".join(item.text.strip() for item in around)[:900]
            ranked.append(
                (
                    _score(text, terms),
                    -order,
                    Finding(
                        kind="recording",
                        title=recording.title,
                        text=text,
                        recording_id=recording.id,
                        start_ms=around[0].start_ms,
                    ),
                )
            )
    # The user's own notebooks in this workspace.
    notes = session.execute(
        select(StudyNote, StudyNotebook)
        .join(StudyNotebook, StudyNotebook.id == StudyNote.notebook_id)
        .where(
            StudyNotebook.workspace_id == workspace_id,
            StudyNotebook.owner_id == user.id,
            or_(
                _matches(StudyNote.text, terms),
                _matches(StudyNote.slide_text, terms),
                _matches(StudyNote.comment, terms),
            ),
        )
        .order_by(StudyNote.created_at.desc())
        .limit(CANDIDATES)
    ).all()
    for order, (note, notebook) in enumerate(notes):
        if note.id == exclude_note_id:
            continue
        parts = [note.clean_text or note.text, note.slide_text, note.comment]
        text = "\n".join(part.strip() for part in parts if part)[:900]
        ranked.append(
            (
                _score(text, terms),
                -order,
                Finding(
                    kind="note",
                    title=note.title or notebook.title,
                    text=text,
                    notebook_id=notebook.id,
                    note_id=note.id,
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    findings: list[Finding] = []
    seen: set[tuple] = set()
    for score, _order, finding in ranked:
        # Overlapping windows of the same recording say the same thing twice.
        key = (
            finding.recording_id,
            (finding.start_ms or 0) // 30_000,
            finding.note_id,
        )
        if score == 0 or key in seen:
            continue
        seen.add(key)
        findings.append(finding)
        if len(findings) == MAX_FINDINGS:
            break
    return findings


def build_compose_input(
    *, question: str, findings: list[Finding], web: str | None
) -> str:
    parts = ["Question: " + question]
    if findings:
        lines = [
            f"[S{number}] ({finding.title}) {finding.text}"
            for number, finding in enumerate(findings, start=1)
        ]
        parts.append("From the workspace:\n" + "\n\n".join(lines))
    else:
        parts.append("From the workspace: nothing was found.")
    if (web or "").strip():
        parts.append("From web research:\n" + web.strip()[:8000])
    return "\n\n".join(parts)


def parse_compose(answer: str) -> tuple[str | None, str | None]:
    data = _json_object(answer)
    short = str(data.get("short_answer") or "").strip()[:MAX_SHORT]
    full = str(data.get("answer") or "").strip()[:MAX_ANSWER]
    return short or None, full or None


def web_sources(items: object) -> list[dict]:
    """Pages the search tool returned (http links only, each once)."""
    sources: list[dict] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not re.match(r"^https?://", url) or url in seen or len(url) > 2000:
            continue
        seen.add(url)
        title = " ".join(str(item.get("title") or "").split())[:200]
        sources.append({"kind": "web", "title": title or url, "url": url})
        if len(sources) == 8:
            break
    return sources
