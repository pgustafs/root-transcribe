"""Meeting notes written by agents, and the agents themselves.

A *note agent* is a named set of instructions ("Summarize the meeting", "List
the tasks"). Running one on a recording's transcript produces a *meeting note*:
a Markdown document that people can then edit and export. The agent runtime
(CUGA) lives in the separate agent-runner container; these tables hold only
definitions and results.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class NoteAgent(Base):
    """Instructions for one kind of note.

    ``owner_id`` NULL = shared with everyone (managed by admins and power
    users); otherwise a personal agent only its owner sees and runs.
    """

    __tablename__ = "note_agent"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # Set on the agents shipped with the app ("summary", "tasks"); their default
    # text lives in services/note_defaults.py so it can be restored.
    builtin_key: Mapped[str | None] = mapped_column(
        String(50), unique=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class MeetingNote(Base):
    """One agent run on one recording, and the document it produced."""

    __tablename__ = "meeting_note"

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
    # The transcript version the note was written from. Loose (no FK): the
    # stamp outlives a superseded revision.
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("note_agent.id", ondelete="SET NULL"), nullable=True
    )
    # Snapshots: the note stays explainable after the agent is edited or deleted.
    agent_name: Mapped[str] = mapped_column(String(120), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    # pending | processing | ready | failed
    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True, nullable=False
    )
    current_step: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # What the agent wrote, kept so an edited note can be restored.
    generated_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What people see and edit.
    content_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optimistic-concurrency counter for edits.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    edited_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
