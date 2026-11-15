"""Database access for study notebooks, sessions, and notes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud import audio_object as audio_object_crud
from app.models.study import StudyNote, StudyNotebook, StudySession
from app.models.user import User

# --- notebooks ----------------------------------------------------------------


async def list_notebooks(
    session: AsyncSession, *, workspace_id: uuid.UUID, owner_id: uuid.UUID
) -> Sequence[StudyNotebook]:
    result = await session.execute(
        select(StudyNotebook)
        .where(
            StudyNotebook.workspace_id == workspace_id,
            StudyNotebook.owner_id == owner_id,
        )
        .order_by(StudyNotebook.updated_at.desc())
    )
    return result.scalars().all()


async def get_notebook(
    session: AsyncSession,
    *,
    notebook_id: uuid.UUID,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> StudyNotebook | None:
    result = await session.execute(
        select(StudyNotebook).where(
            StudyNotebook.id == notebook_id,
            StudyNotebook.workspace_id == workspace_id,
            StudyNotebook.owner_id == owner_id,
        )
    )
    return result.scalars().first()


async def get_notebook_by_source(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    source_key: str,
) -> StudyNotebook | None:
    result = await session.execute(
        select(StudyNotebook).where(
            StudyNotebook.workspace_id == workspace_id,
            StudyNotebook.owner_id == owner_id,
            StudyNotebook.source_key == source_key,
        )
    )
    return result.scalars().first()


async def create_notebook(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    title: str,
    source_url: str | None,
    source_key: str | None,
) -> tuple[StudyNotebook, bool]:
    """The notebook for this source, and whether it was created now.

    One notebook per learner, workspace and source: a second request for the
    same video returns the first notebook (also when two requests race).
    """
    if source_key:
        existing = await get_notebook_by_source(
            session,
            workspace_id=workspace_id,
            owner_id=owner_id,
            source_key=source_key,
        )
        if existing is not None:
            return existing, False
    notebook = StudyNotebook(
        workspace_id=workspace_id,
        owner_id=owner_id,
        title=title,
        source_url=source_url,
        source_key=source_key,
    )
    try:
        async with session.begin_nested():
            session.add(notebook)
            await session.flush()
    except IntegrityError:
        existing = await get_notebook_by_source(
            session,
            workspace_id=workspace_id,
            owner_id=owner_id,
            source_key=source_key or "",
        )
        if existing is None:  # pragma: no cover — the winner's row must exist
            raise
        return existing, False
    return notebook, True


async def note_counts(
    session: AsyncSession, notebook_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not notebook_ids:
        return {}
    result = await session.execute(
        select(StudyNote.notebook_id, func.count())
        .where(StudyNote.notebook_id.in_(notebook_ids))
        .group_by(StudyNote.notebook_id)
    )
    return {row[0]: row[1] for row in result.all()}


async def touch_notebook(session: AsyncSession, notebook_id: uuid.UUID) -> None:
    await session.execute(
        sa_update(StudyNotebook)
        .where(StudyNotebook.id == notebook_id)
        .values(updated_at=datetime.now(UTC))
    )


# --- sessions -----------------------------------------------------------------


async def get_session(
    session: AsyncSession, session_id: uuid.UUID
) -> StudySession | None:
    return await session.get(StudySession, session_id)


async def list_sessions(
    session: AsyncSession, notebook_id: uuid.UUID
) -> Sequence[StudySession]:
    result = await session.execute(
        select(StudySession)
        .where(StudySession.notebook_id == notebook_id)
        .order_by(StudySession.started_at, StudySession.id)
    )
    return result.scalars().all()


async def sessions_for_recording(
    session: AsyncSession, recording_id: uuid.UUID
) -> Sequence[StudySession]:
    result = await session.execute(
        select(StudySession).where(StudySession.recording_id == recording_id)
    )
    return result.scalars().all()


async def owned_session(
    session: AsyncSession, *, session_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[StudySession, StudyNotebook] | None:
    result = await session.execute(
        select(StudySession, StudyNotebook)
        .join(StudyNotebook, StudyNotebook.id == StudySession.notebook_id)
        .where(StudySession.id == session_id, StudyNotebook.owner_id == owner_id)
    )
    row = result.first()
    return (row[0], row[1]) if row else None


# --- notes --------------------------------------------------------------------


async def get_note(session: AsyncSession, note_id: uuid.UUID) -> StudyNote | None:
    return await session.get(StudyNote, note_id)


async def list_notes(
    session: AsyncSession, notebook_id: uuid.UUID
) -> Sequence[StudyNote]:
    """In capture order: sessions by start, then the order within the session."""
    result = await session.execute(
        select(StudyNote)
        .join(StudySession, StudySession.id == StudyNote.session_id)
        .where(StudyNote.notebook_id == notebook_id)
        .order_by(StudySession.started_at, StudySession.id, StudyNote.seq)
    )
    return result.scalars().all()


async def notes_for_sessions(
    session: AsyncSession, session_ids: Sequence[uuid.UUID]
) -> Sequence[StudyNote]:
    if not session_ids:
        return []
    result = await session.execute(
        select(StudyNote).where(StudyNote.session_id.in_(session_ids))
    )
    return result.scalars().all()


async def notes_for_workspace(
    session: AsyncSession, workspace_id: uuid.UUID
) -> Sequence[StudyNote]:
    result = await session.execute(
        select(StudyNote)
        .join(StudyNotebook, StudyNotebook.id == StudyNote.notebook_id)
        .where(StudyNotebook.workspace_id == workspace_id)
    )
    return result.scalars().all()


async def tombstone_media(
    session: AsyncSession,
    notes: Sequence[StudyNote],
    *,
    workspace_id: uuid.UUID,
    reason: str,
    actor: User | None,
) -> None:
    """Ask for durable deletion of the notes' image and audio objects."""
    for note in notes:
        for key, kind in (
            (note.image_key, "study_image"),
            (note.audio_key, "study_audio"),
        ):
            if not key:
                continue
            obj = await audio_object_crud.ensure_for_key(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=key,
                kind=kind,
                ref_id=note.id,
                workspace_id=workspace_id,
            )
            await audio_object_crud.request_delete(
                session, obj, reason=reason, actor=actor
            )


async def delete_sessions(
    session: AsyncSession, session_ids: Sequence[uuid.UUID]
) -> None:
    if session_ids:
        await session.execute(
            sa_delete(StudyNote).where(StudyNote.session_id.in_(session_ids))
        )
        await session.execute(
            sa_delete(StudySession).where(StudySession.id.in_(session_ids))
        )


async def remove_for_recording(
    session: AsyncSession,
    *,
    recording_id: uuid.UUID,
    workspace_id: uuid.UUID,
    actor: User | None,
) -> int:
    """A recording is being deleted: its notes and their media go with it."""
    sessions = await sessions_for_recording(session, recording_id)
    ids = [s.id for s in sessions]
    notes = await notes_for_sessions(session, ids)
    await tombstone_media(
        session,
        notes,
        workspace_id=workspace_id,
        reason="recording_delete",
        actor=actor,
    )
    await delete_sessions(session, ids)
    return len(notes)


# --- sync helpers (Celery worker) ---------------------------------------------


def claim_processing_sync(session: Session, *, note_id: uuid.UUID) -> bool:
    """CAS-claim a pending note (a duplicate task delivery gets False)."""
    result = session.execute(
        sa_update(StudyNote)
        .where(StudyNote.id == note_id, StudyNote.status == "pending")
        .values(status="processing", updated_at=datetime.now(UTC))
    )
    session.commit()
    return result.rowcount == 1


def complete_sync(session: Session, *, note_id: uuid.UUID, text: str) -> None:
    note = session.get(StudyNote, note_id)
    if note is None or note.status != "processing":
        return
    note.text = text or None
    note.status = "ready" if text else "no_speech"
    note.error = None
    session.commit()


def fail_sync(session: Session, *, note_id: uuid.UUID, error: str) -> None:
    note = session.get(StudyNote, note_id)
    if note is None or note.status not in ("pending", "processing"):
        return
    note.status = "failed"
    note.error = error[:2000]
    session.commit()
