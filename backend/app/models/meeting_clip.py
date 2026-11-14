"""Governed audio clips (issue #6, wave D5).

A clip is a bounded excerpt of a meeting recording, cut by the worker with
ffmpeg into its own audio object (kind ``clip``) and played back through the
same authenticated Range proxy as the source recording — never a presigned
URL. Creation is capability-gated (``create_clip``) and, on restricted
recordings, additionally requires an explicit user grant: a clip is an
exfiltration surface, not a convenience.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MeetingClip(Base):
    """One excerpt of a recording, sliced into its own audio object."""

    __tablename__ = "meeting_clip"

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
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # Provenance (review finding): the transcript revision that was on display
    # when the clip was cut. Loose (no FK) — the stamp outlives supersedes.
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending | processing | ready | failed
    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True, nullable=False
    )
    # The sliced object's key once ready (audio_object row kind="clip").
    audio_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
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
