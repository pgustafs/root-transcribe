"""Data access for global in-app notifications."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.notification import Notification


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    payload: dict | None = None,
) -> Notification:
    """Append one notification (the generic hook future event types reuse)."""
    notification = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        link=link,
        payload=payload or {},
    )
    session.add(notification)
    await session.commit()
    await session.refresh(notification)
    return notification


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unread_only: bool = False,
    limit: int = 50,
) -> Sequence[Notification]:
    stmt = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    result = await session.execute(
        stmt.order_by(Notification.created_at.desc()).limit(limit)
    )
    return result.scalars().all()


async def unread_count(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return int(result.scalar_one())


async def get_owned(
    session: AsyncSession, *, notification_id: uuid.UUID, user_id: uuid.UUID
) -> Notification:
    """The caller's notification ("not yours" behaves like "not found")."""
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != user_id:
        raise NotFoundError("Notification not found")
    return notification


async def set_read(
    session: AsyncSession, notification: Notification, *, read: bool
) -> Notification:
    notification.read_at = datetime.now(UTC) if read else None
    await session.commit()
    await session.refresh(notification)
    return notification


async def mark_all_read(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    await session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await session.commit()
