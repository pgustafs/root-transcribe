"""Durable audio-object lifecycle ORM model (issue #6).

Every audio byte in the S3-compatible store gets one row here — meeting
recordings, voiceprint enrollment samples, and (later) clips, redacted
renditions, and export bundles. The row exists so **deletion is a tracked state
machine, not a fire-and-forget call**:

    active → delete_requested → deleting → deleted
                     ↑______________|
                     (retryable failure, backed off)
                                    └→ delete_failed   (dead-letter, admin-visible)

The object ``key`` is preserved until deletion is **confirmed** — application
metadata may never claim erasure after an unclassified storage failure (the
pre-#6 behavior). ``ref_id`` is a polymorphic owner pointer (recording /
voiceprint / …) documented by ``kind``; deliberately no FK, because tombstones
must outlive their owners. ``delete_requested_by_email`` is denormalized so the
audit provenance survives user deletion. Driven by
``tasks.process_audio_deletions``; see ARCHITECTURE.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Why deletion was requested (recorded as audit evidence).
#
# Enforced by crud.audio_object._apply_delete_request. Add reasons here before
# introducing another deletion path so the audit field stays consistent.
AUDIO_DELETE_REASONS = (
    # Retention & data-subject rights
    "retention",
    "user_request",
    "dsar",
    "workspace_delete",
    "recording_delete",
    "clip_deleted",
    # Upload lifecycle (issue #37)
    "upload_failed",
    "orphaned_intent",
    "expired_upload_intent",
    "orphaned_upload_completion",
    "cancelled_upload_completion",
    # Voiceprint enrollment & consent
    "profile_delete",
    "profile_erased",
    "profile_deactivated",
    "failed_enrollment",
    "enrollment_upload_failed",
    "enrollment_cancelled_race",
    "erasure_pending_enrollment",
    "sample_unavailable",
    "consent_withdrawn",
    "consent_no_longer_live",
    "identification_consent_revoked",
    "no_sample_retention_consent",
    # Derived media
    "redacted",
    "redaction_upload_failed",
    "render_failed",
    # Study notes
    "transcribed",
    "notebook_deleted",
)


class AudioObject(Base):
    """One stored audio blob and its durable deletion lifecycle."""

    __tablename__ = "audio_object"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    # Preserved until deletion is CONFIRMED — never cleared on failure.
    # UNIQUE: one lifecycle row per blob is the model's core invariant
    # (review finding: the check-then-insert helper could race two rows in).
    key: Mapped[str] = mapped_column(
        String(512), unique=True, index=True, nullable=False
    )
    # S3 object version (versioned buckets); policy-required erasure must remove
    # every version, and the evidence lands in ``deleted_version_ids``.
    version_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    # Polymorphic owner id (see ``kind``); no FK — the tombstone outlives it.
    ref_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspace.id", ondelete="SET NULL"), nullable=True
    )
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(80), nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Server-side encryption applied at write time (None = store default).
    sse_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kms_key_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default="active",
        index=True,
        nullable=False,
    )
    delete_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Resolved policy identifier behind the request (retention rule, hold, …).
    delete_policy: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delete_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delete_requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    delete_requested_by_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    # Per-CYCLE store-delete failures — drives the retry threshold + backoff and
    # is RESET when a row is re-armed for another deletion cycle (issue #14
    # review), so a fresh blob gets the full retry budget.
    delete_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # CUMULATIVE store-delete failures across every cycle — never reset; audit
    # evidence of how much effort a deletion took (issue #14 review).
    total_delete_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Backoff schedule for retryable failures (None = due immediately).
    next_delete_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    last_delete_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Erasure evidence for versioned buckets: the version ids actually removed.
    deleted_version_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
