"""Pydantic contracts for the speaker directory (voiceprint enrollment).

Response models NEVER expose embeddings or the encrypted voiceprint bytes — only
metadata (counts, model id, timestamps). Enrollment audio is uploaded as multipart,
not modeled here. See ARCHITECTURE.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SpeakerProfileCreate(BaseModel):
    """Create a directory entry for a person."""

    display_name: str = Field(min_length=1, max_length=255)
    # Link to an application user (self-enrollment / known colleague), optional.
    user_id: uuid.UUID | None = None
    # Explicit consent to biometric voice processing (required to enroll prints).
    consent_granted: bool = False
    # Required when asserting consent ON BEHALF of another person (admin only,
    # issue #6): how/where the consent was obtained.
    consent_evidence: str | None = Field(default=None, max_length=1000)


class SpeakerProfileUpdate(BaseModel):
    """Rename or (de)activate a directory entry."""

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = Field(default=None, pattern="^(active|revoked)$")


class SpeakerConsentCreate(BaseModel):
    """Record one consent grant for a purpose (immutable ledger row)."""

    purpose: str = Field(pattern="^(speaker_identification|sample_retention)$")
    # Free-text evidence, REQUIRED when recorded on behalf of another person.
    evidence: str | None = Field(default=None, max_length=1000)
    expires_at: datetime | None = None


class SpeakerConsentWithdraw(BaseModel):
    """Withdraw a consent grant under the profile lock used by enrollment.

    Historical purposes remain withdrawable so existing consent records can
    be revoked. Retention withdrawal also schedules deletion of raw samples.
    An embedding computation already in flight may finish, but its result is
    rejected by the worker's consent recheck before persistence.
    """

    purpose: str = Field(
        pattern="^(speaker_identification|voice_synthesis|sample_retention|"
        "model_improvement)$"
    )
    reason: str | None = Field(default=None, max_length=500)


class SpeakerConsentPublic(BaseModel):
    """One immutable consent-ledger row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    profile_id: uuid.UUID
    purpose: str
    status: str
    policy_version: str
    actor_email: str | None = None
    on_behalf: bool = False
    evidence: dict | None = None
    expires_at: datetime | None = None
    withdrawn_at: datetime | None = None
    withdrawal_reason: str | None = None
    created_at: datetime


class SpeakerProfilePublic(BaseModel):
    """A directory entry with its enrolled-voiceprint count."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    user_id: uuid.UUID | None = None
    status: str
    consent_granted_at: datetime | None = None
    voiceprint_count: int = 0
    # Enrollment samples uploaded but not yet processed into a voiceprint
    # (issue #14 review): during this window ``voiceprint_count`` is still 0,
    # so the erase UI must consider this too — a user must be able to erase a
    # pending enrollment while it is pending.
    pending_enrollment_count: int = 0
    created_by_email: str | None = None
    created_at: datetime
    updated_at: datetime


class ComplianceExportCreate(BaseModel):
    """Request one DSAR bundle for a data subject (admin only)."""

    subject_email: str = Field(min_length=3, max_length=255)
    speaker_profile_id: uuid.UUID | None = None


class ComplianceExportPublic(BaseModel):
    """One DSAR bundle build: job state, then the downloadable artifact."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_email: str
    speaker_profile_id: uuid.UUID | None = None
    status: str
    size_bytes: int | None = None
    error: str | None = None
    requested_by_email: str | None = None
    created_at: datetime
    updated_at: datetime
