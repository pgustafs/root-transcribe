"""Resumable multipart upload sessions for meeting audio (issue #6, wave B1).

A large recording is no longer one memory-buffered POST: the browser opens a
session (which creates the ``uploading`` recording row and an S3 multipart
upload), streams bounded parts with per-part SHA-256 checksums, and completes
explicitly — at which point the recording flips to ``pending`` and the
transcription attempt + outbox row commit atomically. Sessions expire and are
aborted by a beat sweep so abandoned uploads can't accumulate parts forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Every state in which the row still OWNS a store-side multipart upload. Each
# of the three non-`active` ones is a LEASE stamped in `completing_at`:
#
#   completing — a client is finishing the upload
#   expiring   — the expiry sweep is about to abort it
#   cancelling — the user cancelled and the abort is running
#
# `expiring` and `cancelling` are DESTRUCTIVE claims, so no completion claim
# accepts them; the expiry sweep is the last-resort reaper for all three.
# Anything asking "is this session still live?" must use this tuple — a live
# set that misses a state lets another actor abort storage underneath a fresh
# claim (issue #37 review rounds 12-13).
LIVE_UPLOAD_SESSION_STATUSES = ("active", "completing", "expiring", "cancelling")


_LIVE_SQL = "status IN ({})".format(
    ", ".join(f"'{status}'" for status in LIVE_UPLOAD_SESSION_STATUSES)
)


class MeetingUploadSession(Base):
    """One resumable multipart upload of a meeting recording."""

    __tablename__ = "meeting_upload_session"
    __table_args__ = (
        # One live session per recording, DB-enforced.
        Index(
            "uq_meeting_upload_session_active",
            "recording_id",
            unique=True,
            # Every LIVE state counts: the row still owns a store-side
            # multipart upload, so a restart must not open a second session
            # against a recording that is about to be deleted (#37 r12-r13).
            # Keep in step with LIVE_UPLOAD_SESSION_STATUSES.
            sqlite_where=text(_LIVE_SQL),
            postgresql_where=text(_LIVE_SQL),
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
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Indexed: the multipart reconciliation sweep probes ownership by key.
    key: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    # NULL between the intent-first row commit and the store's create_multipart
    # succeeding (review round 4) — a crash in the gap leaves a NULL-id row the
    # expiry sweep closes without an S3 abort.
    s3_upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Server-chosen fixed part size the client must honor (last part may be
    # smaller); bounds API memory per part request.
    part_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    declared_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        server_default="active",
        index=True,
        nullable=False,
    )
    # When a caller claimed the `completing` transition (issue #37 r10). The
    # claim is a LEASE, not a latch: a completion whose owner died mid-flight
    # would otherwise leave the session `completing` forever, and every retry
    # would be told "still in flight" — a permanent false success.
    #: How many client writes (part uploads) are ADMITTED and still running.
    #: Incremented in the same conditional UPDATE that admits the write, so a
    #: deletion fence can never interleave between "is it live?" and "start
    #: writing" — and can WAIT for them to finish (issue #37 review r17b).
    inflight_writes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    #: When the last write was admitted. Bounds a counter leaked by an API
    #: process that died mid-request, so erasure is never blocked forever.
    inflight_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    completing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
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


class MeetingUploadPart(Base):
    """One uploaded part of a session (checksummed; retry-idempotent)."""

    __tablename__ = "meeting_upload_part"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "part_number", name="uq_meeting_upload_part_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meeting_upload_session.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    part_number: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    etag: Mapped[str] = mapped_column(String(80), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
