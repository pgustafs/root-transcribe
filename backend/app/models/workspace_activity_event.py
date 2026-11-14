"""Workspace activity event ORM model.

An append-only history of recording activity, membership and settings changes
that keeps shared workspaces auditable. One row per event, written explicitly
at the route/worker call sites
(:mod:`app.services.workspace_activity`).

``payload`` is a small JSON dict of ids + display names (enough to render one
feed line without joins); the actor's email is denormalized so history survives
user deletion (audit-log pattern).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkspaceActivityEvent(Base):
    """One thing that happened in a workspace."""

    __tablename__ = "workspace_activity_event"
    __table_args__ = (
        Index("ix_workspace_activity_ws_created", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Dotted event name, e.g. "source.uploaded", "member.role_changed".
    event_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
