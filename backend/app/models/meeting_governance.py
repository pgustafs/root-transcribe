"""Meeting permission grants + audit-grade event trail (issue #6, wave E1).

``MeetingPermissionGrant`` overrides the code-level default capability map per
workspace: allow/deny one capability for a workspace role or a specific user
(user rows win). ``MeetingAuditEvent`` is the immutable record of sensitive
audio operations (listen, download, export, approve, delete, permission
changes) — deliberately WITHOUT foreign keys so evidence survives the deletion
of the workspace, recording, or actor it describes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MeetingPermissionGrant(Base):
    """One per-workspace capability override (role- or user-scoped)."""

    __tablename__ = "meeting_permission_grant"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "role",
            "user_id",
            "capability",
            name="uq_meeting_permission_grant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Exactly one of (role, user_id): a workspace-role default override, or a
    # person-specific override (which wins).
    role: Mapped[str | None] = mapped_column(String(10), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    capability: Mapped[str] = mapped_column(String(40), nullable=False)
    allow: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class MeetingAuditEvent(Base):
    """Append-only audit record of one sensitive audio operation."""

    __tablename__ = "meeting_audit_event"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Plain ids, no FKs: audit evidence must outlive its subjects.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    recording_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # audio.listen | audio.download | transcript.export | revision.approve |
    # revision.discard | recording.delete | suggestion.accept |
    # permission.change | …
    action: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    target: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
        nullable=False,
    )


class RetentionPolicy(Base):
    """Per-artifact retention override (issue #6, E3).

    Resolution: workspace row → deployment row → static settings default.
    ``retention_days`` null means "keep indefinitely" (explicitly chosen, not
    an accident of omission). Legal hold trumps every tier.
    """

    __tablename__ = "retention_policy"
    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_id", "artifact", name="uq_retention_policy_scope"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # deployment | workspace
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    # The API currently accepts raw_audio only; old rows remain auditable.
    artifact: Mapped[str] = mapped_column(String(30), nullable=False)
    retention_days: Mapped[int | None] = mapped_column(nullable=True)
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


class LegalHold(Base):
    """An active legal hold blocking EVERY purge path for its scope (E3)."""

    __tablename__ = "legal_hold"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # workspace | recording | speaker_profile — plain id, no FK: a hold must
    # keep meaning even while its subject resists deletion.
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=False
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
