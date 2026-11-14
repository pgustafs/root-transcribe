"""Meeting transcription attempt ORM model (issue #6).

One row per transcription execution stores idempotency and model provenance:


- the worker claims an attempt with an atomic ``pending → running`` CAS, so a
  duplicate delivery (acks_late redelivery, reconciler resend) no-ops;
- ``heartbeat_at`` lets the reconciler fail attempts whose worker died;
- a partial unique index enforces at most one live attempt per recording;
- model/config columns record what actually ran (audit + future revisions).

``enqueue_count`` counts outbox sends for this attempt (the reconciler's
resend dedupe key), bounded by ``MEETING_ATTEMPT_MAX_ENQUEUES``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

ATTEMPT_ACTIVE_STATUSES = ("pending", "running")


class MeetingTranscriptionAttempt(Base):
    """One idempotent transcription execution of a meeting recording."""

    __tablename__ = "meeting_transcription_attempt"
    __table_args__ = (
        UniqueConstraint(
            "recording_id", "attempt_number", name="uq_transcription_attempt_number"
        ),
        # Enforce one live attempt per recording with a partial unique index
        # on both supported test and deployment database dialects.
        Index(
            "uq_transcription_attempt_active",
            "recording_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_recording.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # "{recording_id}:{attempt_number}" — stable across redeliveries.
    idempotency_key: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False
    )
    # pending | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default="pending",
        index=True,
        nullable=False,
    )
    current_step: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Cooperative stop flag, checked between pipeline steps.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Provenance of what actually ran (stamped by the worker).
    stt_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    diarization_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Code-level pipeline identifier, bumped on algorithm changes.
    pipeline_version: Mapped[str] = mapped_column(
        String(50), default="v1", server_default="v1", nullable=False
    )
    # Run inputs snapshot (filename hint, threshold overrides, …).
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Outbox sends for this attempt (reconciler resend generation counter).
    enqueue_count: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Stable, safe failure classification (issue #37); mirrors the recording's
    # error_code. One of media_validation.MediaErrorCode; NULL on legacy rows.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The machine_candidate revision this attempt produced (plain Uuid — the
    # revision row carries the FK side of the relationship via attempt_id).
    produced_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
