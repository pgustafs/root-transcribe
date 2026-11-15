"""Repository for speaker-name runs (async routes / sync worker)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.speaker_name_run import SpeakerNameRun


async def latest(
    session: AsyncSession, *, recording_id: uuid.UUID, revision_id: uuid.UUID | None
) -> SpeakerNameRun | None:
    """The newest run on this version of the transcript."""
    if revision_id is None:
        return None
    stmt = (
        select(SpeakerNameRun)
        .where(
            SpeakerNameRun.recording_id == recording_id,
            SpeakerNameRun.revision_id == revision_id,
        )
        .order_by(SpeakerNameRun.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def get(
    session: AsyncSession, *, recording_id: uuid.UUID, run_id: uuid.UUID
) -> SpeakerNameRun | None:
    run = await session.get(SpeakerNameRun, run_id)
    return run if run is not None and run.recording_id == recording_id else None


def claim_processing_sync(session: Session, *, run_id: uuid.UUID) -> bool:
    """CAS-claim a pending run (duplicate task delivery no-ops on False)."""
    result = session.execute(
        sa_update(SpeakerNameRun)
        .where(SpeakerNameRun.id == run_id, SpeakerNameRun.status == "pending")
        .values(status="processing", updated_at=datetime.now(UTC))
    )
    session.commit()
    return result.rowcount == 1


def complete_sync(
    session: Session,
    *,
    run_id: uuid.UUID,
    suggestions: list[dict],
    model: str | None,
    duration_ms: int,
) -> None:
    run = session.get(SpeakerNameRun, run_id)
    if run is None or run.status != "processing":
        return
    run.suggestions = suggestions
    run.model = model
    run.duration_ms = duration_ms
    run.status = "ready"
    run.error = None
    session.commit()


def fail_sync(session: Session, *, run_id: uuid.UUID, error: str) -> None:
    run = session.get(SpeakerNameRun, run_id)
    if run is None or run.status not in ("pending", "processing"):
        return
    run.status = "failed"
    run.error = error[:2000]
    session.commit()
