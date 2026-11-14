"""Persist workspace and recording activity with async and sync sessions."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.workspace_activity_event import WorkspaceActivityEvent


async def record_activity(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor: User | None,
    event_type: str,
    payload: dict | None = None,
) -> None:
    """Append one event (commits — call after the operation it describes)."""
    session.add(
        WorkspaceActivityEvent(
            workspace_id=workspace_id,
            actor_id=actor.id if actor else None,
            actor_email=actor.email if actor else None,
            event_type=event_type,
            payload=payload or {},
        )
    )
    await session.commit()


def record_activity_sync(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    actor_email: str | None,
    event_type: str,
    payload: dict | None = None,
) -> None:
    """Sync variant for the Celery worker (actor passed as raw id/email)."""
    session.add(
        WorkspaceActivityEvent(
            workspace_id=workspace_id,
            actor_id=actor_id,
            actor_email=actor_email,
            event_type=event_type,
            payload=payload or {},
        )
    )
    session.commit()
