"""A cleaned copy of a transcript, proposed by the language model.

The speech model mishears words; the language model reads the transcript and
proposes corrections, line by line (see ``services/transcript_cleanup.py`` for
what it may change). Nothing is applied to the transcript: the proposals live
here, beside it, and a person can dismiss any of them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class TranscriptCleanup(Base):
    __tablename__ = "transcript_cleanup"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_recording.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The transcript version that was read. Loose (no FK), like notes.
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # pending | processing | ready | failed
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # [{segment_id, original, text, status}] for the lines the model corrected;
    # status is suggested | dismissed. ``original`` is the line as it was read,
    # so a later edit of the transcript shows as a mismatch instead of being
    # silently overwritten in the cleaned view.
    changes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Proposals that were rewrites, not corrections, and were dropped.
    rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
