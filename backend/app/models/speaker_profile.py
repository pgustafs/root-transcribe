"""Speaker directory ORM models — enrolled people and their voiceprints.

To turn anonymous diarization labels ("Speaker 1/2") into real names, the org
maintains a **speaker directory**: a :class:`SpeakerProfile` per person, each with
one or more :class:`SpeakerVoiceprint` enrollments (a speaker embedding computed
from a consented voice sample). Identification compares a diarized speaker's
embedding against these voiceprints by cosine similarity.

**Voiceprints are biometric data** and are governed accordingly: a profile records
explicit ``consent_granted_at`` and an optional ``retention_delete_at`` (honored by
the retention sweep); the embedding is stored **encrypted at rest** and is never
returned by the API. ``user_id`` links a profile to an application user when the
speaker is one (self-enrollment); org guests can be enrolled without an account.
See ARCHITECTURE.md for the governance model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class SpeakerProfile(Base):
    """One enrolled person in the org speaker directory."""

    __tablename__ = "speaker_profile"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Linked application user when the speaker is one (self-enrollment); NULL for
    # enrolled non-users (external participants).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # active | revoked (revoked keeps history but is excluded from matching).
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", nullable=False
    )
    # Explicit biometric-processing consent; matching requires it to be set.
    consent_granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Optional per-profile retention: the sweep purges voiceprints past this.
    retention_delete_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    voiceprints: Mapped[list[SpeakerVoiceprint]] = relationship(
        "SpeakerVoiceprint",
        back_populates="profile",
        cascade="all, delete-orphan",
    )


class SpeakerVoiceprint(Base):
    """One enrolled voice embedding for a :class:`SpeakerProfile`.

    Multiple voiceprints per profile improve robustness (different rooms / mics);
    identification compares against the mean (centroid) of a profile's active
    voiceprints. ``embedding`` is an **encrypted** float32 vector blob; ``dims``
    and ``model_id`` record its shape/provenance so a model change can invalidate
    stale prints. The consented enrollment sample lives in the audio object store
    (``sample_key``), kept only if audio retention allows.
    """

    __tablename__ = "speaker_voiceprint"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    speaker_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("speaker_profile.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Encrypted float32 embedding (see app/services/speaker_directory.py). Never
    # serialized to the API.
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dims: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # --- Embedding contract (issue #6): a print may only be compared against
    # meeting embeddings produced by the EXACT same model + revision +
    # preprocessing; anything else is incomparable and needs re-enrollment.
    model_revision: Mapped[str | None] = mapped_column(String(100), nullable=True)
    preprocessing_version: Mapped[str] = mapped_column(
        String(20), default="v1", server_default="v1", nullable=False
    )
    sample_rate: Mapped[int] = mapped_column(
        Integer, default=16000, server_default="16000", nullable=False
    )
    # Which biometric keyring key encrypted ``embedding`` ("legacy" = the
    # SECRET_KEY-derived key; rotation re-encrypts under the active key id).
    key_id: Mapped[str] = mapped_column(
        String(50), default="legacy", server_default="legacy", nullable=False
    )
    # Object-store key of the enrollment audio sample (nullable / purgeable).
    sample_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Inert historical column retained for database compatibility. This app does
    # not synthesize voices or populate reference transcripts.
    ref_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    profile: Mapped[SpeakerProfile] = relationship(
        "SpeakerProfile", back_populates="voiceprints"
    )
