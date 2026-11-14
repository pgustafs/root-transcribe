"""Immutable transcript revisions + append-only edit events (issue #6).

A recording's transcript is no longer one mutable segment set: every pipeline
run persists a NEW revision (``machine_candidate``) and reprocessing can never
overwrite an approved or human-edited transcript again — the pre-#6 retry
deleted every segment, destroying corrections. States:

    machine_candidate → in_review → approved → superseded
                              └───→ discarded

Approving a revision supersedes the previously approved one (a partial unique
index allows only one ``approved`` per recording). The recording tracks its
``latest_revision_id`` (newest non-discarded) and ``approved_revision_id``.
``MeetingTranscriptEditEvent`` records human changes, speaker assignments,
approvals and carry-over merges.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
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


class MeetingTranscriptRevision(Base):
    """One immutable version of a recording's transcript."""

    __tablename__ = "meeting_transcript_revision"
    __table_args__ = (
        UniqueConstraint(
            "recording_id", "revision_number", name="uq_transcript_revision_number"
        ),
        # At most one approved revision per recording, DB-enforced.
        Index(
            "uq_transcript_revision_approved",
            "recording_id",
            unique=True,
            sqlite_where=text("state = 'approved'"),
            postgresql_where=text("state = 'approved'"),
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
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Lineage (previous latest revision at creation time). Plain Uuid — a
    # self-FK adds nothing but delete-ordering constraints here.
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(20),
        default="machine_candidate",
        server_default="machine_candidate",
        index=True,
        nullable=False,
    )
    # pipeline (a transcription attempt) | migration (pre-#6 backfill) |
    # redaction (a masked copy of an approved revision).
    origin: Mapped[str] = mapped_column(
        String(20), default="pipeline", server_default="pipeline", nullable=False
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meeting_transcription_attempt.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Carry-over merge summary: edits copied from the previous revision, and
    # human-touched segments that found no confident target (need review).
    carried_edits: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    conflict_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Redaction revisions: audio ranges muted in the redacted rendition
    # ([{start_ms, end_ms, category}]) — wave E4.
    audio_redactions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class MeetingTranscriptEditEvent(Base):
    """Append-only record of one human change to a transcript revision."""

    __tablename__ = "meeting_transcript_edit_event"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_transcript_revision.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # SET NULL so history survives future segment operations; segment_idx keeps
    # the position readable either way.
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meeting_transcript_segment.id", ondelete="SET NULL"),
        nullable=True,
    )
    segment_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # text_edit | speaker_assign | speaker_clear | bulk_assign | merge |
    # approve | discard | redact_text
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
