"""Background-job ledger repository.

Async reads back the admin Jobs API; the sync upserts are called from the Celery
signal handlers (:mod:`app.worker.signals`) via the sync engine. Each upsert is
get-or-create by ``task_id`` so handlers are order-independent (a ``started`` can
arrive before the ``pending`` row exists, e.g. when enqueued elsewhere).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.background_job import BackgroundJob
from app.schemas.job import JobSummary

# Statuses that mean the job is finished (eligible for retention pruning).
TERMINAL_STATUSES = ("success", "failure", "revoked")

_TRACEBACK_MAX = 10_000


def _runtime_ms(started_at: datetime | None, finished_at: datetime) -> int | None:
    """Milliseconds between start and finish, tolerant of naive DB datetimes.

    SQLite returns naive datetimes (Postgres returns tz-aware); assume UTC for any
    naive value so subtracting an aware ``finished_at`` doesn't raise.
    """
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return int((finished_at - started_at).total_seconds() * 1000)


# --- async (API) ---


async def list_jobs(
    session: AsyncSession,
    *,
    status: str | None = None,
    task_name: str | None = None,
    q: str | None = None,
    skip: int = 0,
    limit: int = 100,
) -> Sequence[BackgroundJob]:
    """Return jobs newest-first, optionally filtered. ``q`` matches id/request id."""
    stmt = select(BackgroundJob)
    if status:
        stmt = stmt.where(BackgroundJob.status == status)
    if task_name:
        stmt = stmt.where(BackgroundJob.task_name == task_name)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                BackgroundJob.task_id.ilike(like),
                BackgroundJob.request_id.ilike(like),
            )
        )
    stmt = stmt.order_by(BackgroundJob.created_at.desc()).offset(skip).limit(limit)
    return (await session.execute(stmt)).scalars().all()


async def get_by_task_id(
    session: AsyncSession, *, task_id: str
) -> BackgroundJob | None:
    """Fetch one job by its Celery task id."""
    return (
        await session.execute(
            select(BackgroundJob).where(BackgroundJob.task_id == task_id)
        )
    ).scalar_one_or_none()


async def mark_revoked(session: AsyncSession, *, task_id: str) -> BackgroundJob | None:
    """Mark a job revoked from the API (admin cancel). Returns the updated row.

    Sets ``revoked`` and stamps ``finished_at``/``runtime_ms`` if not already
    terminal. The Celery ``task_revoked`` signal also records this when the worker
    discards the task, but we update here too for immediate UI feedback and for a
    ``pending`` task that may never reach a worker.
    """
    job = await get_by_task_id(session, task_id=task_id)
    if job is None:
        return None
    job.status = "revoked"
    if job.finished_at is None:
        job.finished_at = datetime.now(UTC)
        job.runtime_ms = _runtime_ms(job.started_at, job.finished_at)
    await session.commit()
    await session.refresh(job)
    return job


async def summary(session: AsyncSession) -> JobSummary:
    """Per-status counts (drives the header cards + whether the UI keeps polling)."""
    rows = (
        await session.execute(
            select(BackgroundJob.status, func.count()).group_by(BackgroundJob.status)
        )
    ).all()
    counts = {status: int(n) for status, n in rows}
    total = sum(counts.values())
    active = any(counts.get(state, 0) for state in ("pending", "started", "retry"))
    return JobSummary(
        total=total,
        active=active,
        pending=counts.get("pending", 0),
        started=counts.get("started", 0),
        success=counts.get("success", 0),
        failure=counts.get("failure", 0),
        retry=counts.get("retry", 0),
        revoked=counts.get("revoked", 0),
    )


# --- sync (Celery signal handlers) ---


def _insert_if_absent(session: Session, task_id: str, task_name: str | None) -> bool:
    """Insert the ``pending`` row unless one exists; ``False`` if unsupported.

    ``ON CONFLICT (task_id) DO NOTHING`` is the one form that stays correct
    when two processes race: under PostgreSQL the loser waits for the winner's
    transaction and then inserts nothing, instead of raising.
    """
    values = {
        "id": uuid.uuid4(),
        "task_id": task_id,
        "task_name": task_name or "unknown",
        "status": "pending",
        "retries": 0,
        "created_at": datetime.now(UTC),
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        statement: Any = pg_insert(BackgroundJob).values(values)
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        statement = sqlite_insert(BackgroundJob).values(values)
    else:
        return False
    session.execute(statement.on_conflict_do_nothing(index_elements=["task_id"]))
    return True


def _get_or_create(
    session: Session, task_id: str, task_name: str | None
) -> BackgroundJob:
    """The ledger row for ``task_id``, created ``pending`` when it is missing.

    The publish handler (in the enqueuing process, e.g. beat) and the prerun
    handler (in the worker) run within milliseconds of each other for the same
    task. A plain look-up-then-add lets both miss the row and both insert; the
    loser's unique violation was logged and swallowed by the signal handler,
    which left ``worker`` and ``started_at`` empty for a task that ran.
    """
    by_task_id = select(BackgroundJob).where(BackgroundJob.task_id == task_id)
    job = session.execute(by_task_id).scalar_one_or_none()
    if job is not None:
        return job
    if _insert_if_absent(session, task_id, task_name):
        # Ours or the racing writer's: either way it exists and is visible now.
        return session.execute(by_task_id).scalar_one()
    job = BackgroundJob(
        task_id=task_id, task_name=task_name or "unknown", status="pending"
    )
    session.add(job)
    return job


def record_published(
    session: Session,
    *,
    task_id: str,
    task_name: str | None,
    request_id: str | None = None,
    triggered_by: str | None = None,
    queue: str | None = None,
    args_summary: str | None = None,
) -> None:
    """Record an enqueued task (status stays ``pending``)."""
    job = _get_or_create(session, task_id, task_name)
    if task_name:
        job.task_name = task_name
    if request_id:
        job.request_id = request_id
    if triggered_by:
        job.triggered_by = triggered_by
    if queue:
        job.queue = queue
    if args_summary is not None:
        job.args_summary = args_summary


def record_started(
    session: Session,
    *,
    task_id: str,
    task_name: str | None = None,
    worker: str | None = None,
    retries: int = 0,
) -> None:
    """Mark a task as running."""
    job = _get_or_create(session, task_id, task_name)
    job.status = "started"
    job.started_at = datetime.now(UTC)
    if worker:
        job.worker = worker
    job.retries = retries


def record_finished(
    session: Session,
    *,
    task_id: str,
    status: str,
    result_summary: str | None = None,
) -> None:
    """Mark a task terminal (success/failure/...) and compute its runtime."""
    job = _get_or_create(session, task_id, None)
    job.status = status
    job.finished_at = datetime.now(UTC)
    job.runtime_ms = _runtime_ms(job.started_at, job.finished_at)
    if result_summary is not None:
        job.result_summary = result_summary


def record_failure(
    session: Session,
    *,
    task_id: str,
    error: str | None,
    traceback: str | None,
) -> None:
    """Enrich a failed task with its error + traceback."""
    job = _get_or_create(session, task_id, None)
    job.status = "failure"
    if job.finished_at is None:
        job.finished_at = datetime.now(UTC)
        job.runtime_ms = _runtime_ms(job.started_at, job.finished_at)
    if error:
        job.error = error[:2000]
    if traceback:
        job.traceback = traceback[:_TRACEBACK_MAX]


def record_retry(session: Session, *, task_id: str) -> None:
    """Mark a task as retrying."""
    job = _get_or_create(session, task_id, None)
    job.status = "retry"
    job.retries = (job.retries or 0) + 1


def record_revoked(session: Session, *, task_id: str) -> None:
    """Mark a task as revoked."""
    job = _get_or_create(session, task_id, None)
    job.status = "revoked"
    job.finished_at = datetime.now(UTC)


def delete_terminal_older_than(session: Session, cutoff: datetime) -> int:
    """Delete terminal job rows created before ``cutoff``; return the row count."""
    result = session.execute(
        delete(BackgroundJob).where(
            BackgroundJob.status.in_(TERMINAL_STATUSES),
            BackgroundJob.created_at < cutoff,
        )
    )
    return result.rowcount or 0
