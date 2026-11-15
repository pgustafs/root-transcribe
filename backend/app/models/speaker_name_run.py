"""One "find the speakers' names in the transcript" run and what it proposed.

The language model reads the transcript for introductions ("my name is Peter",
"jag heter Anna") and proposes a name per unnamed speaker, each with the quote
it is based on. Nothing is applied by the run: a person accepts or dismisses
every suggestion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class SpeakerNameRun(Base):
    __tablename__ = "speaker_name_run"

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
    # [{speaker_label, name, quote, start_ms, confidence, status}] where status
    # is pending | accepted | dismissed.
    suggestions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
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
