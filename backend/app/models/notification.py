"""Global per-user notification ORM model.

A reusable in-app notification: today seeded by workspace-comment **@mentions**
(``type="mention"``); future event types add rows through the same
:func:`app.crud.notification.create` without schema changes. ``link`` is an SPA
path the notification drawer navigates to; ``read_at`` marks it read (the
masthead badge counts unread rows).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Notification(Base):
    """One in-app notification for one user."""

    __tablename__ = "notification"
    __table_args__ = (
        Index("ix_notification_user_read_created", "user_id", "read_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # "mention" today; new types reuse this table.
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # SPA path the drawer navigates to, e.g. /workspaces/{id}?note=…&comment=….
    link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
