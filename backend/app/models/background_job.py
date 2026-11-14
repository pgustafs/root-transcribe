"""Background-job ledger ORM model.

A durable record of every Celery task, written automatically by the signal
handlers in :mod:`app.worker.signals` (no per-task code). Powers the admin Jobs
page so operators can see what ran, what's running, what failed, how long it took,
and trace a job back to the logs via ``task_id`` (the Celery id) and ``request_id``
(the correlation id woven through the app logs). Read-only to the app; rows are
pruned by ``tasks.cleanup_old_jobs`` after ``JOBS_RETENTION_DAYS``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BackgroundJob(Base):
    """One Celery task execution, from enqueue to terminal state."""

    __tablename__ = "background_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The Celery task UUID — the "job number" to trace in logs / Flower.
    task_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    # Dotted task name, e.g. "tasks.run_ingest_pipeline".
    task_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    # pending | started | success | failure | retry | revoked
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True, nullable=False
    )
    # Correlation id shared with the access logs / the task's own log records.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Email of the user who triggered the job, when known at enqueue time.
    triggered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker: Mapped[str | None] = mapped_column(String(255), nullable=True)
    queue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Redacted/truncated views — never raw payloads (no base64, no secrets).
    args_summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    runtime_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
