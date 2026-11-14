"""Task-outbox repository (issue #6).

Async ``enqueue`` is called inside a route's transaction so the send intent
commits atomically with the domain rows; the sync helpers drive the
``tasks.dispatch_task_outbox`` beat. See :mod:`app.models.task_outbox`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.task_outbox import TaskOutbox

# Backoff cap: a broker outage retries at most this far apart.
_MAX_BACKOFF_SECONDS = 15 * 60


async def enqueue(
    session: AsyncSession,
    *,
    task_name: str,
    queue: str,
    payload: dict,
    headers: dict | None = None,
    dedupe_key: str | None = None,
) -> TaskOutbox:
    """Record a send intent in the caller's transaction (flushed, not committed)."""
    row = TaskOutbox(
        task_name=task_name,
        queue=queue,
        payload=payload,
        headers=headers,
        dedupe_key=dedupe_key,
    )
    session.add(row)
    await session.flush()
    return row


def enqueue_sync(
    session: Session,
    *,
    task_name: str,
    queue: str,
    payload: dict,
    headers: dict | None = None,
    dedupe_key: str | None = None,
) -> TaskOutbox | None:
    """Sync enqueue (worker/reconciler); a dedupe-key duplicate is skipped and
    returns None (SAVEPOINT so the surrounding transaction survives)."""
    row = TaskOutbox(
        task_name=task_name,
        queue=queue,
        payload=payload,
        headers=headers,
        dedupe_key=dedupe_key,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        return None
    return row


def claim_due_sync(
    session: Session, *, now: datetime, limit: int = 100
) -> list[TaskOutbox]:
    """Due pending rows for one dispatcher pass (SKIP LOCKED on PostgreSQL so
    concurrent dispatchers never double-send)."""
    stmt = (
        select(TaskOutbox)
        .where(
            TaskOutbox.status == "pending",
            or_(
                TaskOutbox.next_attempt_at.is_(None),
                TaskOutbox.next_attempt_at <= now,
            ),
        )
        .order_by(TaskOutbox.created_at)
        .limit(limit)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return list(session.execute(stmt).scalars().all())


def mark_sent_sync(session: Session, row: TaskOutbox, *, now: datetime) -> None:
    row.status = "sent"
    row.sent_at = now
    row.last_error = None
    session.flush()


def record_failure_sync(
    session: Session,
    row: TaskOutbox,
    *,
    error: str,
    now: datetime,
    max_attempts: int,
    base_backoff_seconds: int,
    dead_letter: bool = True,
) -> bool:
    """Record a failed send; returns True when dead-lettered.

    ``dead_letter=False`` rows never go ``dead`` (review round 6): a
    key-bearing task's row is the liveness marker protecting external state
    (a queued enrollment sample), and dead-lettering it after N broker
    failures would strip that protection exactly when the outage is at its
    worst — they keep retrying at the max backoff instead.
    """
    row.attempts += 1
    row.last_error = error[:2000]
    if dead_letter and row.attempts >= max_attempts:
        row.status = "dead"
        session.flush()
        return True
    backoff = min(
        base_backoff_seconds * (2 ** (row.attempts - 1)), _MAX_BACKOFF_SECONDS
    )
    row.next_attempt_at = now + timedelta(seconds=backoff)
    session.flush()
    return False


async def consume(session: AsyncSession, *, dedupe_key: str) -> int:
    """Async twin of :func:`consume_sync` — retire an outbox row whose work has
    been cancelled (e.g. an enrollment erased before the worker ran, issue #14
    review), in the caller's transaction. Otherwise the dispatcher would keep
    re-sending cancelled work through a broker outage."""
    rows = (
        (
            await session.execute(
                select(TaskOutbox).where(TaskOutbox.dedupe_key == dedupe_key)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await session.delete(row)
    return len(rows)


def consume_sync(session: Session, *, dedupe_key: str) -> int:
    """Delete the delivered row for finished work (uncommitted; the caller's
    transaction decides).

    Sent rows mark queued-but-unprocessed work — the orphan-intent sweep
    treats any audio key they reference as live (review round 4). Workers
    call this in the SAME transaction that lands the work's result, so the
    marker disappears exactly when the protection stops being needed.
    """
    rows = (
        session.execute(select(TaskOutbox).where(TaskOutbox.dedupe_key == dedupe_key))
        .scalars()
        .all()
    )
    for row in rows:
        session.delete(row)
    if rows:
        session.flush()
    return len(rows)


def prune_sent_sync(
    session: Session,
    *,
    older_than: datetime,
    exclude_task_names: tuple[str, ...] = (),
) -> int:
    """Drop delivered rows past the retention window (housekeeping).

    ``exclude_task_names`` rows are NEVER pruned by age (review round 5):
    their sent rows are liveness markers for queued work holding external
    state (enrollment samples), and only the worker's consumption or the
    reconciler's verdict may remove them — a retention clock must not.
    """
    stmt = select(TaskOutbox).where(
        TaskOutbox.status == "sent", TaskOutbox.sent_at < older_than
    )
    if exclude_task_names:
        stmt = stmt.where(TaskOutbox.task_name.not_in(exclude_task_names))
    rows = session.execute(stmt).scalars().all()
    for row in rows:
        session.delete(row)
    if rows:
        session.flush()
    return len(rows)
