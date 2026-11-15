"""Transcript-segment ORM model — one speaker turn of a diarized transcript.

A recording's transcript is stored as an ordered list of segments, each a
contiguous span of speech attributed to one diarized speaker (``speaker_label``,
e.g. ``SPEAKER_00``). When a segment's speaker is identified against the enrolled
voiceprint directory, ``speaker_profile_id`` + ``speaker_confidence`` are set (an
above-threshold, human-confirmable match). The rendered Markdown transcript that
becomes a workspace source is assembled from these rows, so editing a speaker
label or the text here is the system of record. See ARCHITECTURE.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MeetingTranscriptSegment(Base):
    """One speaker turn (aligned diarization + transcription) of a recording."""

    __tablename__ = "meeting_transcript_segment"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_recording.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The immutable revision this segment belongs to (issue #6) — a recording's
    # segments span every revision; readers always filter by one revision.
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_transcript_revision.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Optimistic-concurrency counter for human edits (expected_version → 409,
    # the WorkspaceNote.version pattern).
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    # 0-based position within the recording (render + display order).
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Anonymous diarization label from pyannote, e.g. "SPEAKER_00".
    speaker_label: Mapped[str] = mapped_column(String(64), nullable=False)
    # Identified person (nullable — anonymous until matched/confirmed).
    speaker_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("speaker_profile.id", ondelete="SET NULL"), nullable=True
    )
    # A name a person typed (or accepted from a suggestion) for this speaker in
    # this recording. An annotation only: no directory entry, no voiceprint.
    # Shown in preference to the directory person; setting one clears the other.
    speaker_display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Cosine similarity of the match (None when anonymous or human-assigned).
    speaker_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # True once a human edited the text or (re)assigned the speaker here.
    is_edited: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    edited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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

    recording: Mapped[MeetingRecording] = relationship(  # noqa: F821
        "MeetingRecording", back_populates="segments"
    )
