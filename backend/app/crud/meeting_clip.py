"""Repository for governed audio clips (async routes / sync worker)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.meeting_clip import MeetingClip
from app.models.user import User


async def create_pending(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    title: str,
    start_ms: int,
    end_ms: int,
    actor: User,
    revision_id: uuid.UUID | None = None,
) -> MeetingClip:
    """Create a ``pending`` clip (flushed, not committed — the caller commits
    it atomically with its outbox send-intent)."""
    clip = MeetingClip(
        workspace_id=workspace_id,
        recording_id=recording_id,
        title=title,
        start_ms=start_ms,
        end_ms=end_ms,
        revision_id=revision_id,
        status="pending",
        created_by_id=actor.id,
        created_by_email=actor.email,
    )
    session.add(clip)
    await session.flush()
    return clip


async def list_for_recording(
    session: AsyncSession, *, recording_id: uuid.UUID
) -> Sequence[MeetingClip]:
    stmt = (
        select(MeetingClip)
        .where(MeetingClip.recording_id == recording_id)
        .order_by(MeetingClip.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_for_recording(
    session: AsyncSession, *, recording_id: uuid.UUID, clip_id: uuid.UUID
) -> MeetingClip | None:
    clip = await session.get(MeetingClip, clip_id)
    if clip is None or clip.recording_id != recording_id:
        return None
    return clip


# --- sync helpers (Celery worker) ------------------------------------------


def claim_processing_sync(session: Session, *, clip_id: uuid.UUID) -> bool:
    """CAS-claim a pending clip (duplicate task delivery no-ops on False)."""
    result = session.execute(
        sa_update(MeetingClip)
        .where(MeetingClip.id == clip_id, MeetingClip.status == "pending")
        .values(status="processing", updated_at=datetime.now(UTC))
    )
    session.commit()
    return result.rowcount == 1


def complete_sync(
    session: Session, *, clip_id: uuid.UUID, audio_key: str, size_bytes: int
) -> None:
    clip = session.get(MeetingClip, clip_id)
    if clip is None or clip.status != "processing":
        return
    clip.audio_key = audio_key
    clip.size_bytes = size_bytes
    clip.status = "ready"
    clip.error = None
    session.commit()


def fail_sync(session: Session, *, clip_id: uuid.UUID, error: str) -> None:
    clip = session.get(MeetingClip, clip_id)
    if clip is None or clip.status not in ("pending", "processing"):
        return
    clip.status = "failed"
    clip.error = error[:2000]
    session.commit()
