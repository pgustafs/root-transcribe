"""DSAR / compliance export jobs (issue #6, wave E5).

One job = one data-subject access request bundle: everything the meetings
domain holds about a person (consent ledger, voiceprint METADATA — never the
embeddings —, appearances across recordings with revision + time ranges,
deletion evidence from the audio-object lifecycle, and their audit events),
zipped by the worker and stored as an ``audio_object(kind="export_bundle")``
so the bundle itself lives inside the governed deletion lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ComplianceExportJob(Base):
    """One admin-requested DSAR bundle build."""

    __tablename__ = "compliance_export_job"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The data subject: an email (matched against users, profiles, audit
    # actors) and/or one specific speaker profile.
    subject_email: Mapped[str] = mapped_column(String(255), nullable=False)
    speaker_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # pending | running | completed | failed
    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True, nullable=False
    )
    audio_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    requested_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
