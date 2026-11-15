"""Study notes: screenshots with a short transcript, taken while recording a tab.

* ``StudyNotebook`` — one per learner, workspace and source (a video). Private
  to its owner.
* ``StudySession`` — one recording visit. Its id is made by the browser, because
  notes are saved while the meeting recording does not exist on the server yet;
  ``recording_id`` is filled in when that recording is uploaded.
* ``StudyNote`` — one capture: a PNG frame and the transcript of a short piece
  of tab audio. The id is made by the browser too, so a repeated upload is the
  same note.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: What the learner can mark a note with at the press.
NOTE_TAGS = ("important", "unclear")
#: none = not asked for (or no model) | pending | processing | ready | failed
ENRICH_STATUSES = ("none", "pending", "processing", "ready", "failed")

NOTE_STATUSES = (
    "pending",
    "processing",
    "ready",
    "no_speech",
    "failed",
    "screenshot_only",
)


def _now() -> datetime:
    return datetime.now(UTC)


class StudyNotebook(Base):
    __tablename__ = "study_notebook"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "workspace_id", "source_key", name="uq_study_notebook_source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # The link exactly as the learner pasted it.
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Stable identity of the source, e.g. "youtube:dQw4w9WgXcQ". NULL = the
    # notebook is only found by picking it.
    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class StudySession(Base):
    __tablename__ = "study_session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    notebook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("study_notebook.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Loose (no FK): set when the recording is uploaded; note media is removed
    # explicitly when that recording is deleted.
    recording_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    language: Mapped[str] = mapped_column(String(10), default="auto", nullable=False)
    # until_stopped | last_15s
    mode: Mapped[str] = mapped_column(
        String(20), default="until_stopped", nullable=False
    )
    # The learner asked for notes to be worked on by the language model
    # (title, tidy text, key terms, text on the slide).
    enrich: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class StudyNote(Base):
    __tablename__ = "study_note"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_study_note_session_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    notebook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("study_notebook.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("study_session.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Capture order within the session (0, 1, 2 …).
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # Times on the recorder's clock ("in this recording"), in milliseconds.
    captured_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Temporary: removed once the excerpt has been transcribed.
    audio_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # The owner's own words about this note, kept apart from the transcript.
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Marks set by the learner: a list of NOTE_TAGS.
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # --- written by the language model, never by the learner ---
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # ``text`` without fillers and with sentence breaks; ``text`` stays as said.
    clean_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What can be read in the image (Markdown): text, formulas, code.
    slide_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"term": "...", "definition": "..."}]
    terms: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # A plain-language explanation, for notes marked "unclear".
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrich_status: Mapped[str] = mapped_column(
        String(20), default="none", nullable=False
    )
    enrich_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # How many times the model work was queued (dedupe key of the task).
    enrich_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Why a note that was still open ended (paused, stopped, source_changed,
    # sharing_lost). NULL = the learner stopped it.
    end_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # A link to this moment in the source, only when the learner pasted one.
    source_time_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # How many times transcription was queued (first upload = 1, each retry +1).
    send_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
