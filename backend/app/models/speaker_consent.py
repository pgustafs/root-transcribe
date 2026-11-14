"""Purpose-scoped biometric consent records + speaker-identity review queue (#6).

``SpeakerConsentRecord`` replaces the single ``consent_granted_at`` timestamp
with an immutable, append-only ledger: one row per consent act, carrying the
purpose, policy version, evidence, scope, expiry, and withdrawal. The current
state of a (profile, purpose) pair is its **latest** row — nothing is ever
edited, so the ledger doubles as audit/DSAR evidence.

``SpeakerSuggestion`` is the human-review queue for uncertain speaker
identification: when a match clears the score floor but not the calibrated
auto-assign gate (absolute threshold + top-1/top-2 margin), the pipeline leaves
the speaker anonymous and records a suggestion for a person to confirm — an
uncertain guess must never silently name someone in the meeting record.
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
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SpeakerConsentRecord(Base):
    """One immutable consent act for one purpose."""

    __tablename__ = "speaker_consent_record"
    # Resolving the current status of a (profile, purpose) pair — and the
    # retention sweep folding history per purpose — reads the LATEST row, so a
    # composite (profile_id, purpose, created_at) index keeps that a bounded
    # lookup as the immutable ledger grows (issue #14 review).
    __table_args__ = (
        Index(
            "ix_consent_profile_purpose_created",
            "profile_id",
            "purpose",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("speaker_profile.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    # granted | withdrawn — the latest row per (profile, purpose) wins.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    # Hash of the exact consent text shown/asserted (evidence without storing
    # free text that might drift).
    consent_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Who recorded this act (the subject for self-consent).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # True when an administrator asserted consent for another person — requires
    # elevated authorization and recorded evidence (enforced at the route).
    on_behalf: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Method/source evidence ({"method": "verbal", "note": …, "document": …}).
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Scope: null = organization-wide; else limited to one workspace.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspace.id", ondelete="SET NULL"), nullable=True
    )
    region: Mapped[str | None] = mapped_column(String(20), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawal_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class SpeakerSuggestion(Base):
    """One uncertain speaker-identification match awaiting human review."""

    __tablename__ = "speaker_suggestion"
    __table_args__ = (
        UniqueConstraint(
            "revision_id", "speaker_label", name="uq_speaker_suggestion_revision_label"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_recording.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_transcript_revision.id", ondelete="CASCADE"),
        nullable=False,
    )
    speaker_label: Mapped[str] = mapped_column(String(64), nullable=False)
    suggested_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("speaker_profile.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    margin: Mapped[float] = mapped_column(Float, nullable=False)
    # How many clean turns contributed to the score (evidence of sample size).
    sample_turns: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending | accepted | rejected
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default="pending",
        index=True,
        nullable=False,
    )
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
