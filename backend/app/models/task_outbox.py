"""Transactional task-outbox ORM model (issue #6).

Enqueueing background work used to be a dual write: commit the domain row,
then call ``apply_async`` — a crash or broker outage between the two stranded
the row (e.g. a ``pending`` recording no worker would ever pick up), and an
ephemeral Redis broker loses accepted messages on restart.

An outbox row is written **in the same transaction** as the domain rows, then
delivered to Celery by an after-commit fast path and the
``tasks.dispatch_task_outbox`` beat. ``id`` doubles as the Celery ``task_id``
so broker-level duplicates are recognizable; the real duplicate guard is the
consumer-side CAS (e.g. ``MeetingTranscriptionAttempt.mark_running``).
``sent`` only means "the broker accepted the message once" — recovery from a
broker wipe is the reconciler's job, not this table's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TaskOutbox(Base):
    """One durable "send this Celery task" intent."""

    __tablename__ = "task_outbox"

    # Doubles as the Celery task_id at delivery time.
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_name: Mapped[str] = mapped_column(String(150), nullable=False)
    queue: Mapped[str] = mapped_column(String(50), nullable=False)
    # Task kwargs (JSON-serializable only, like all Celery args here).
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Correlation headers ({"request_id": …, "user": …}).
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Idempotent-enqueue guard (e.g. "attempt:{id}:send:{n}") — a concurrent
    # writer of the same intent is silently dropped.
    dedupe_key: Mapped[str | None] = mapped_column(
        String(160), unique=True, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default="pending",
        index=True,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Backoff schedule for failed sends (None = due immediately).
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
