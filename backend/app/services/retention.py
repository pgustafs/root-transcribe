"""Retention resolution + legal-hold checks (issue #6, wave E3).

Sync-only (the purge paths run in Celery beats and delete routes). Resolution
order for an artifact's retention window: workspace policy row → deployment
policy row → static settings default. A live legal hold on the recording, its
workspace, or a speaker profile trumps everything.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.meeting_governance import LegalHold, RetentionPolicy

# Workspace overrides currently govern recording audio only. Voiceprint expiry
# belongs to the speaker profile; transcript/clip policies are not implemented.
RETENTION_ARTIFACTS = ("raw_audio",)


def resolve_retention_days_sync(
    session: Session, *, artifact: str, workspace_id: uuid.UUID | None
) -> int | None:
    """The effective retention window in days (None = keep indefinitely)."""
    if workspace_id is not None:
        row = session.execute(
            select(RetentionPolicy).where(
                RetentionPolicy.scope_type == "workspace",
                RetentionPolicy.scope_id == workspace_id,
                RetentionPolicy.artifact == artifact,
            )
        ).scalar_one_or_none()
        if row is not None:
            return row.retention_days
    row = session.execute(
        select(RetentionPolicy).where(
            RetentionPolicy.scope_type == "deployment",
            RetentionPolicy.artifact == artifact,
        )
    ).scalar_one_or_none()
    if row is not None:
        return row.retention_days
    if artifact != "raw_audio":
        return None
    days = settings.AUDIO_RETENTION_DAYS
    return days if days > 0 else None


def is_held_sync(session: Session, *scopes: tuple[str, uuid.UUID | None]) -> bool:
    """True when ANY (scope_type, scope_id) pair carries an active hold."""
    pairs = [(t, i) for t, i in scopes if i is not None]
    if not pairs:
        return False
    rows = session.execute(
        select(LegalHold.scope_type, LegalHold.scope_id).where(
            LegalHold.released_at.is_(None)
        )
    ).all()
    active = {(t, i) for t, i in rows}
    return any(pair in active for pair in pairs)
