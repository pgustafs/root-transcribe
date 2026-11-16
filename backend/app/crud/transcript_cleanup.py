"""Repository for cleaned transcripts (async routes / sync worker)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.transcript_cleanup import TranscriptCleanup


async def latest(
    session: AsyncSession, *, recording_id: uuid.UUID, revision_id: uuid.UUID | None
) -> TranscriptCleanup | None:
    """The newest cleanup of this version of the transcript."""
    if revision_id is None:
        return None
    stmt = (
        select(TranscriptCleanup)
        .where(
            TranscriptCleanup.recording_id == recording_id,
            TranscriptCleanup.revision_id == revision_id,
        )
        .order_by(TranscriptCleanup.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def get(
    session: AsyncSession, *, recording_id: uuid.UUID, cleanup_id: uuid.UUID
) -> TranscriptCleanup | None:
    cleanup = await session.get(TranscriptCleanup, cleanup_id)
    if cleanup is None or cleanup.recording_id != recording_id:
        return None
    return cleanup


def claim_processing_sync(session: Session, *, cleanup_id: uuid.UUID) -> bool:
    """CAS-claim a pending cleanup (duplicate task delivery no-ops on False)."""
    result = session.execute(
        sa_update(TranscriptCleanup)
        .where(
            TranscriptCleanup.id == cleanup_id, TranscriptCleanup.status == "pending"
        )
        .values(status="processing", updated_at=datetime.now(UTC))
    )
    session.commit()
    return result.rowcount == 1


def set_step_sync(session: Session, *, cleanup_id: uuid.UUID, step: str) -> None:
    cleanup = session.get(TranscriptCleanup, cleanup_id)
    if cleanup is not None and cleanup.status == "processing":
        cleanup.current_step = step
        session.commit()


def complete_sync(
    session: Session,
    *,
    cleanup_id: uuid.UUID,
    changes: list[dict],
    rejected: int,
    model: str | None,
    duration_ms: int,
) -> None:
    cleanup = session.get(TranscriptCleanup, cleanup_id)
    if cleanup is None or cleanup.status != "processing":
        return
    cleanup.changes = changes
    cleanup.rejected = rejected
    cleanup.model = model
    cleanup.duration_ms = duration_ms
    cleanup.status = "ready"
    cleanup.current_step = None
    cleanup.error = None
    session.commit()


def fail_sync(session: Session, *, cleanup_id: uuid.UUID, error: str) -> None:
    cleanup = session.get(TranscriptCleanup, cleanup_id)
    if cleanup is None or cleanup.status not in ("pending", "processing"):
        return
    cleanup.status = "failed"
    cleanup.current_step = None
    cleanup.error = error[:2000]
    session.commit()
