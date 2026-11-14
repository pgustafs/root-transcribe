"""Grants + audit-event repository (issue #6, wave E1)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meeting_governance import MeetingAuditEvent, MeetingPermissionGrant
from app.models.user import User


async def list_grants(
    session: AsyncSession, workspace_id: uuid.UUID
) -> Sequence[MeetingPermissionGrant]:
    result = await session.execute(
        select(MeetingPermissionGrant)
        .where(MeetingPermissionGrant.workspace_id == workspace_id)
        .order_by(MeetingPermissionGrant.created_at)
    )
    return result.scalars().all()


async def set_grant(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    capability: str,
    allow: bool,
    role: str | None,
    user_id: uuid.UUID | None,
    actor: User,
) -> MeetingPermissionGrant:
    """Upsert one override row (flushed; caller commits)."""
    result = await session.execute(
        select(MeetingPermissionGrant).where(
            MeetingPermissionGrant.workspace_id == workspace_id,
            MeetingPermissionGrant.capability == capability,
            MeetingPermissionGrant.role == role,
            MeetingPermissionGrant.user_id == user_id,
        )
    )
    grant = result.scalar_one_or_none()
    if grant is None:
        grant = MeetingPermissionGrant(
            workspace_id=workspace_id,
            capability=capability,
            role=role,
            user_id=user_id,
            created_by_id=actor.id,
            created_by_email=actor.email,
        )
        session.add(grant)
    grant.allow = allow
    await session.flush()
    return grant


async def delete_grant(
    session: AsyncSession, *, workspace_id: uuid.UUID, grant_id: uuid.UUID
) -> bool:
    result = await session.execute(
        select(MeetingPermissionGrant).where(
            MeetingPermissionGrant.id == grant_id,
            MeetingPermissionGrant.workspace_id == workspace_id,
        )
    )
    grant = result.scalar_one_or_none()
    if grant is None:
        return False
    await session.delete(grant)
    await session.flush()
    return True


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    actor: User,
    workspace_id: uuid.UUID | None = None,
    recording_id: uuid.UUID | None = None,
    target: dict | None = None,
    request_id: str | None = None,
) -> None:
    """Append one immutable audit row (flushed with the caller's commit)."""
    session.add(
        MeetingAuditEvent(
            workspace_id=workspace_id,
            recording_id=recording_id,
            actor_id=actor.id,
            actor_email=actor.email,
            action=action,
            target=target,
            request_id=request_id,
        )
    )
    await session.flush()


# --- Legal holds + retention policies (issue #6, E3) ---


async def place_hold(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: uuid.UUID,
    reason: str,
    actor: User,
):
    from app.models.meeting_governance import LegalHold

    hold = LegalHold(
        scope_type=scope_type,
        scope_id=scope_id,
        reason=reason,
        created_by_id=actor.id,
        created_by_email=actor.email,
    )
    session.add(hold)
    await session.flush()
    return hold


async def active_hold(
    session: AsyncSession, *, scope_pairs: list[tuple[str, uuid.UUID]]
):
    """The first active hold matching any (scope_type, scope_id) pair."""
    from app.models.meeting_governance import LegalHold

    result = await session.execute(
        select(LegalHold).where(LegalHold.released_at.is_(None))
    )
    active = {(h.scope_type, h.scope_id): h for h in result.scalars()}
    for pair in scope_pairs:
        if pair in active:
            return active[pair]
    return None


async def workspace_has_active_hold(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> bool:
    """True when the workspace, ANY of its recordings, or any speaker profile
    referenced by a hold in its scope is under an active legal hold.

    One EXISTS query (review round 2): the previous caller-built scope list
    only covered recordings that still had audio, so a held transcript whose
    raw audio was already purged escaped the guard.
    """
    from app.models.meeting_governance import LegalHold
    from app.models.meeting_recording import MeetingRecording

    recording_ids = select(MeetingRecording.id).where(
        MeetingRecording.workspace_id == workspace_id
    )
    stmt = select(LegalHold.id).where(
        LegalHold.released_at.is_(None),
        ((LegalHold.scope_type == "workspace") & (LegalHold.scope_id == workspace_id))
        | (
            (LegalHold.scope_type == "recording")
            & (LegalHold.scope_id.in_(recording_ids))
        ),
    )
    return (await session.execute(stmt.limit(1))).first() is not None


async def release_hold(session: AsyncSession, hold, *, actor: User) -> None:
    from datetime import UTC, datetime

    hold.released_at = datetime.now(UTC)
    hold.released_by_email = actor.email
    await session.flush()


async def list_retention_policies(session: AsyncSession, workspace_id: uuid.UUID):
    from app.models.meeting_governance import RetentionPolicy

    result = await session.execute(
        select(RetentionPolicy).where(
            RetentionPolicy.scope_type == "workspace",
            RetentionPolicy.scope_id == workspace_id,
        )
    )
    return result.scalars().all()


async def set_retention_policy(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    retention_days: int | None,
    actor: User,
):
    from app.models.meeting_governance import RetentionPolicy

    result = await session.execute(
        select(RetentionPolicy).where(
            RetentionPolicy.scope_type == "workspace",
            RetentionPolicy.scope_id == workspace_id,
            RetentionPolicy.artifact == artifact,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = RetentionPolicy(
            scope_type="workspace",
            scope_id=workspace_id,
            artifact=artifact,
            created_by_id=actor.id,
            created_by_email=actor.email,
        )
        session.add(row)
    row.retention_days = retention_days
    await session.flush()
    return row
