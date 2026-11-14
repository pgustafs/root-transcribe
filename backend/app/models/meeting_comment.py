"""Meeting transcript comments (issue #6, wave D4).

A direct copy of the workspace-note comment shape (one-level threads, resolved
state on roots, denormalized author, ``mentioned_user_ids``), with transcript
anchors instead of a quote-only pin: ``at_ms`` points at a moment of the
recording (click-to-seek) and ``segment_id`` loosely references the segment the
comment was made on. Both are plain values — a comment must survive its segment
being superseded by a newer revision, so the quote carries the robust context.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MeetingComment(Base):
    """One comment on a meeting transcript (root, or a reply in its thread)."""

    __tablename__ = "meeting_comment"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_recording.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Denormalized for permission checks in one hop (note-comment pattern).
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # One-level threads: replies point at their root comment.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meeting_comment.id", ondelete="CASCADE"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Transcript anchors: a moment (ms) and the segment commented on. Loose
    # (no FK) — the comment outlives revision supersedes and re-transcription.
    at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # The transcript text this comment is about (robust to later edits).
    anchor_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mentioned_user_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
