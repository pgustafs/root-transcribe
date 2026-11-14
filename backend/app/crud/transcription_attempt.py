"""Transcription-attempt repository (issue #6).

Async helpers back the API (create an attempt inside the enqueue transaction);
sync helpers drive worker claims, heartbeats, completion and reconciliation. See
:mod:`app.models.meeting_transcription_attempt`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.meeting_recording import MeetingRecording
from app.models.meeting_transcription_attempt import (
    ATTEMPT_ACTIVE_STATUSES,
    MeetingTranscriptionAttempt,
)
from app.models.user import User

# --- async (API) ---


async def create(
    session: AsyncSession,
    *,
    recording_id: uuid.UUID,
    actor: User,
    config: dict | None = None,
) -> MeetingTranscriptionAttempt:
    """Add the next attempt for a recording (flushed, caller commits with its
    transaction). Raises ``IntegrityError`` if a live attempt already exists —
    the DB-enforced one-in-flight guard."""
    result = await session.execute(
        select(func.max(MeetingTranscriptionAttempt.attempt_number)).where(
            MeetingTranscriptionAttempt.recording_id == recording_id
        )
    )
    number = (result.scalar() or 0) + 1
    attempt = MeetingTranscriptionAttempt(
        recording_id=recording_id,
        attempt_number=number,
        idempotency_key=f"{recording_id}:{number}",
        config=config,
        created_by_id=actor.id,
        created_by_email=actor.email,
    )
    session.add(attempt)
    await session.flush()
    return attempt


async def list_for_recording(
    session: AsyncSession, recording_id: uuid.UUID
) -> list[MeetingTranscriptionAttempt]:
    """Every attempt ever made for a recording, oldest first.

    The attempt ledger IS the exactly-once record: one row per enqueue, with
    ``enqueue_count`` counting outbox sends for that row and the ``pending →
    running`` CAS ensuring only one of them executes. Exposing it lets an
    operator (and the e2e suite) answer "was this delivered once?" without a
    database session (issue #37).
    """
    result = await session.execute(
        select(MeetingTranscriptionAttempt)
        .where(MeetingTranscriptionAttempt.recording_id == recording_id)
        .order_by(MeetingTranscriptionAttempt.attempt_number)
    )
    return list(result.scalars().all())


# --- sync (Celery worker) ---


def get_sync(
    session: Session, attempt_id: uuid.UUID
) -> MeetingTranscriptionAttempt | None:
    return session.get(MeetingTranscriptionAttempt, attempt_id)


def mark_running(session: Session, attempt_id: uuid.UUID) -> bool:
    """Atomically claim a **pending** attempt → ``running``.

    The single ``UPDATE … WHERE status='pending'`` is the compare-and-swap that
    makes duplicate delivery safe: only the delivery whose UPDATE matches a row
    proceeds; a redelivered/resent duplicate sees rowcount 0 and bails.
    """
    now = datetime.now(UTC)
    result = session.execute(
        update(MeetingTranscriptionAttempt)
        .where(
            MeetingTranscriptionAttempt.id == attempt_id,
            MeetingTranscriptionAttempt.status == "pending",
        )
        .values(status="running", error=None, started_at=now, heartbeat_at=now)
    )
    session.commit()
    return result.rowcount == 1


def heartbeat_sync(
    session: Session, attempt_id: uuid.UUID, *, step: str | None = None
) -> None:
    attempt = session.get(MeetingTranscriptionAttempt, attempt_id)
    if attempt is not None:
        attempt.heartbeat_at = datetime.now(UTC)
        if step is not None:
            attempt.current_step = step
        session.commit()


def finish_sync(
    session: Session,
    attempt_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    error_code: str | None = None,
    stt_model: str | None = None,
    diarization_model: str | None = None,
    embedding_model_id: str | None = None,
    commit: bool = True,
) -> bool:
    """CAS the attempt ``running`` → terminal; returns whether we won.

    ``commit=False`` lets the caller commit the CAS in the SAME transaction as
    the result it acknowledges (review round 2: acknowledging completion in
    its own transaction left a terminal attempt with no persisted result if
    the process died before the follow-up transaction).

    A worker that lost its claim (the stale-heartbeat reconciler already
    failed the attempt, possibly starting a retry) gets ``False`` and must
    NOT write results onto the recording — pre-review this silently
    overwrote a reclaimed attempt's failed state as completed.
    """
    values: dict = {
        "status": status,
        "error": error,
        "error_code": error_code,
        "current_step": None,
        "finished_at": datetime.now(UTC),
    }
    if stt_model is not None:
        values["stt_model"] = stt_model
    if diarization_model is not None:
        values["diarization_model"] = diarization_model
    if embedding_model_id is not None:
        values["embedding_model_id"] = embedding_model_id
    result = session.execute(
        update(MeetingTranscriptionAttempt)
        .where(
            MeetingTranscriptionAttempt.id == attempt_id,
            MeetingTranscriptionAttempt.status == "running",
        )
        .values(**values)
    )
    if commit:
        session.commit()
    return result.rowcount == 1


def list_stuck_pending_sync(
    session: Session, *, now: datetime, older_than_seconds: int
) -> list[MeetingTranscriptionAttempt]:
    """Pending attempts past the start timeout — their broker message is lost
    (send failed after commit, or the broker was wiped)."""
    cutoff = now - timedelta(seconds=older_than_seconds)
    return list(
        session.execute(
            select(MeetingTranscriptionAttempt).where(
                MeetingTranscriptionAttempt.status == "pending",
                MeetingTranscriptionAttempt.created_at < cutoff,
            )
        )
        .scalars()
        .all()
    )


def list_stale_running_sync(
    session: Session, *, now: datetime, stale_after_seconds: int
) -> list[MeetingTranscriptionAttempt]:
    """Running attempts whose worker went silent past the heartbeat window."""
    cutoff = now - timedelta(seconds=stale_after_seconds)
    return list(
        session.execute(
            select(MeetingTranscriptionAttempt).where(
                and_(
                    MeetingTranscriptionAttempt.status == "running",
                    MeetingTranscriptionAttempt.heartbeat_at.is_not(None),
                    MeetingTranscriptionAttempt.heartbeat_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )


def list_orphaned_pending_recordings_sync(
    session: Session, *, now: datetime, older_than_seconds: int
) -> list[MeetingRecording]:
    """Aged ``pending`` recordings with no live attempt (pre-#6 rows, or an
    attempt that was cancelled out from under them) — backstop so nothing stays
    pending forever."""
    cutoff = now - timedelta(seconds=older_than_seconds)
    active = select(MeetingTranscriptionAttempt.recording_id).where(
        MeetingTranscriptionAttempt.status.in_(ATTEMPT_ACTIVE_STATUSES)
    )
    return list(
        session.execute(
            select(MeetingRecording).where(
                MeetingRecording.status == "pending",
                MeetingRecording.created_at < cutoff,
                MeetingRecording.id.not_in(active),
            )
        )
        .scalars()
        .all()
    )
