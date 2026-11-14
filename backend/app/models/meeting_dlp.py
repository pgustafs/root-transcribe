"""DLP terms and findings for meeting transcripts (issue #6, wave E4).

``DlpTerm`` is one workspace-defined detection pattern (literal or regex).
``DlpFinding`` is one detector hit inside one segment of one revision —
detection only proposes; a human accepts before redaction, and a redacted
finding keeps its row (status ``redacted``) as the audit trail of *why* the
superseding revision masked that span.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DlpTerm(Base):
    """One workspace-defined DLP pattern."""

    __tablename__ = "dlp_term"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class DlpFinding(Base):
    """One detector hit awaiting human review (or its redaction record)."""

    __tablename__ = "dlp_finding"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_recording.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_transcript_revision.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_transcript_segment.id", ondelete="CASCADE"),
        nullable=False,
    )
    # email | phone | card | secret | term
    detector: Mapped[str] = mapped_column(String(20), nullable=False)
    term_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    matched_text: Mapped[str] = mapped_column(String(500), nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    # The segment's time range — the audio span a redaction would mute.
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending | accepted | redacting (claimed by a worker) |
    # dismissed | redacted
    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
