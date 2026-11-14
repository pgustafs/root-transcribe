"""Global in-app notifications — the masthead bell.

Per-user rows (today: workspace-comment @mentions) listed newest first; the
badge polls ``/unread-count``. All operations are scoped to the caller's own
rows ("not yours" behaves like "not found").
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentUserDep
from app.crud import notification as notification_crud
from app.db.session import SessionDep
from app.schemas.notification import NotificationPublic, NotificationUnreadCount

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    session: SessionDep,
    user: CurrentUserDep,
    unread_only: bool = False,
    limit: int = 50,
) -> list[NotificationPublic]:
    """The caller's notifications, newest first."""
    rows = await notification_crud.list_for_user(
        session,
        user_id=user.id,
        unread_only=unread_only,
        limit=min(max(limit, 1), 100),
    )
    return [NotificationPublic.model_validate(n) for n in rows]


@router.get("/unread-count")
async def unread_count(
    session: SessionDep, user: CurrentUserDep
) -> NotificationUnreadCount:
    """The unread total for the masthead badge (cheap indexed count)."""
    count = await notification_crud.unread_count(session, user_id=user.id)
    return NotificationUnreadCount(count=count)


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(session: SessionDep, user: CurrentUserDep) -> None:
    await notification_crud.mark_all_read(session, user_id=user.id)


@router.post("/{notification_id}/read")
async def mark_read(
    session: SessionDep, user: CurrentUserDep, notification_id: uuid.UUID
) -> NotificationPublic:
    notification = await notification_crud.get_owned(
        session, notification_id=notification_id, user_id=user.id
    )
    notification = await notification_crud.set_read(session, notification, read=True)
    return NotificationPublic.model_validate(notification)


@router.post("/{notification_id}/unread")
async def mark_unread(
    session: SessionDep, user: CurrentUserDep, notification_id: uuid.UUID
) -> NotificationPublic:
    notification = await notification_crud.get_owned(
        session, notification_id=notification_id, user_id=user.id
    )
    notification = await notification_crud.set_read(session, notification, read=False)
    return NotificationPublic.model_validate(notification)
