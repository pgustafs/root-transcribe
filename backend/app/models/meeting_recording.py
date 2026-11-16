"""A workspace recording, audio reference and transcription lifecycle.

The row is created before transcription is enqueued. Resumable uploads start in
``uploading``; completed uploads become ``pending`` and progress through
``diarizing``, ``transcribing`` and ``identifying`` to ``completed`` or ``failed``.
The UI polls this row and reads segments from its selected transcript revision.

Audio bytes live in S3; ``audio_key`` is an opaque storage reference. The saved
``requested_language`` is independent of the provider-reported ``language``.
``created_by_email`` preserves attribution after user deletion. See ARCHITECTURE.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Terminal + in-flight statuses, in pipeline order (see the worker task).


class MeetingRecording(Base):
    """One workspace meeting recording and its transcription state."""

    __tablename__ = "meeting_recording"
    __table_args__ = (
        Index("ix_meeting_recording_ws_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # pending | uploading | transcribing | diarizing | identifying | completed | failed
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    # Pipeline step in flight (live progress for the UI), e.g. "diarize".
    current_step: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Opaque object-store key for the audio blob (None until uploaded / purged).
    # Indexed: the multipart reconciliation sweep probes ownership by key.
    audio_key: Mapped[str | None] = mapped_column(
        String(512), index=True, nullable=True
    )
    audio_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # User-selected input language; independent of the detected output language.
    requested_language: Mapped[str] = mapped_column(
        String(8), default="auto", server_default="auto", nullable=False
    )
    # How many people speak, when the user knows it (1-10); NULL lets speaker
    # detection decide. A wrong automatic count invents a speaker out of quiet
    # or overlapping speech, or merges two people.
    expected_speakers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stable, safe failure classification (issue #37): one of the
    # media_validation.MediaErrorCode values. NULL on legacy rows (which carry
    # only the free-text ``error``) and on non-failed recordings. The UI branches
    # on this code, never on the human-readable ``error`` string.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Privacy-safe recorder context supplied by the client at upload completion
    # (issue #37 r7): closed browser/container/codec tokens plus bounded
    # assembly/timing aggregates. The WORKER attaches these to the media
    # rejection it emits, so the rejection signal never depends on a
    # client-submitted verdict (which would be forgeable and replayable).
    client_diagnostics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Whether the recorder attested that meeting participants were notified /
    # consented to recording (surfaced in the UI; an org-policy flag).
    participant_consent: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    # Attestation evidence (issue #6, B5): which policy text was attested, how
    # ("attested_checkbox" | "upload_attestation"), by whom, and when — audit
    # provenance beyond the bare boolean.
    consent_policy_version: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    consent_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    consent_actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consent_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set by the retention sweep once the audio blob is purged (transcript kept).
    audio_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    # --- Transcript revisions (issue #6). Plain Uuid pointers (no FK) to avoid
    # a circular constraint with meeting_transcript_revision.recording_id;
    # revisions are only ever deleted by the recording's own CASCADE.
    # Newest non-discarded revision (what the UI shows when nothing is approved).
    latest_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # The authoritative revision (at most one; enforced on the revision table).
    approved_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # Sensitivity label (issue #6, E2): internal | confidential | restricted.
    # ``restricted`` requires explicit user grants for download/export/clip
    # operations by non-admin callers, in addition to normal capabilities.
    sensitivity: Mapped[str] = mapped_column(
        String(20), default="internal", server_default="internal", nullable=False
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

    segments: Mapped[list[MeetingTranscriptSegment]] = relationship(
        "MeetingTranscriptSegment",
        back_populates="recording",
        cascade="all, delete-orphan",
        order_by="MeetingTranscriptSegment.idx",
    )


# Imported at the bottom to populate the relationship string reference without a
# circular import at module load.
from app.models.meeting_transcript_segment import (  # noqa: E402
    MeetingTranscriptSegment,
)
