"""Repository for note agents and meeting notes (async routes / sync worker)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.meeting_note import MeetingNote, NoteAgent
from app.models.user import User

# --- agents ------------------------------------------------------------------


async def list_agents_for(session: AsyncSession, user: User) -> Sequence[NoteAgent]:
    """Shared agents plus the caller's own: built-ins first, then by name."""
    stmt = (
        select(NoteAgent)
        .where(or_(NoteAgent.owner_id.is_(None), NoteAgent.owner_id == user.id))
        .order_by(
            NoteAgent.owner_id.is_not(None),
            NoteAgent.builtin_key.is_(None),
            NoteAgent.name,
        )
    )
    return (await session.execute(stmt)).scalars().all()


async def get_agent_for(
    session: AsyncSession, user: User, agent_id: uuid.UUID
) -> NoteAgent | None:
    """An agent the caller may see: shared, or their own."""
    agent = await session.get(NoteAgent, agent_id)
    if agent is None or agent.owner_id not in (None, user.id):
        return None
    return agent


async def create_agent(
    session: AsyncSession,
    *,
    actor: User,
    shared: bool,
    name: str,
    description: str | None,
    instructions: str,
) -> NoteAgent:
    agent = NoteAgent(
        owner_id=None if shared else actor.id,
        name=name,
        description=description,
        instructions=instructions,
        created_by_id=actor.id,
    )
    session.add(agent)
    await session.flush()
    return agent


# --- notes -------------------------------------------------------------------


async def create_pending(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    revision_id: uuid.UUID | None,
    agent: NoteAgent,
    actor: User,
) -> MeetingNote:
    """Create a ``pending`` note (flushed, not committed — the caller commits
    it atomically with its outbox send-intent)."""
    note = MeetingNote(
        workspace_id=workspace_id,
        recording_id=recording_id,
        revision_id=revision_id,
        agent_id=agent.id,
        agent_name=agent.name,
        instructions=agent.instructions,
        status="pending",
        created_by_id=actor.id,
        created_by_email=actor.email,
    )
    session.add(note)
    await session.flush()
    return note


async def list_for_recording(
    session: AsyncSession, *, recording_id: uuid.UUID
) -> Sequence[MeetingNote]:
    stmt = (
        select(MeetingNote)
        .where(MeetingNote.recording_id == recording_id)
        .order_by(MeetingNote.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_for_recording(
    session: AsyncSession, *, recording_id: uuid.UUID, note_id: uuid.UUID
) -> MeetingNote | None:
    note = await session.get(MeetingNote, note_id)
    if note is None or note.recording_id != recording_id:
        return None
    return note


def apply_edit(note: MeetingNote, *, content: str, actor: User) -> None:
    """Replace the visible text and bump the version (caller commits)."""
    note.content_markdown = content
    note.version += 1
    note.edited_by_email = actor.email
    note.edited_at = datetime.now(UTC)


def restore_generated(note: MeetingNote) -> None:
    """Back to what the agent wrote (caller commits)."""
    note.content_markdown = note.generated_markdown
    note.version += 1
    note.edited_by_email = None
    note.edited_at = None


# --- sync helpers (Celery worker) ---------------------------------------------


def claim_processing_sync(session: Session, *, note_id: uuid.UUID) -> bool:
    """CAS-claim a pending note (duplicate task delivery no-ops on False)."""
    result = session.execute(
        sa_update(MeetingNote)
        .where(MeetingNote.id == note_id, MeetingNote.status == "pending")
        .values(status="processing", updated_at=datetime.now(UTC))
    )
    session.commit()
    return result.rowcount == 1


def set_step_sync(session: Session, *, note_id: uuid.UUID, step: str) -> None:
    note = session.get(MeetingNote, note_id)
    if note is not None and note.status == "processing":
        note.current_step = step
        session.commit()


def complete_sync(
    session: Session,
    *,
    note_id: uuid.UUID,
    markdown: str,
    model: str | None,
    duration_ms: int,
) -> None:
    note = session.get(MeetingNote, note_id)
    if note is None or note.status != "processing":
        return
    note.generated_markdown = markdown
    note.content_markdown = markdown
    note.model = model
    note.duration_ms = duration_ms
    note.status = "ready"
    note.current_step = None
    note.error = None
    session.commit()


def fail_sync(session: Session, *, note_id: uuid.UUID, error: str) -> None:
    note = session.get(MeetingNote, note_id)
    if note is None or note.status not in ("pending", "processing"):
        return
    note.status = "failed"
    note.current_step = None
    note.error = error[:2000]
    session.commit()
