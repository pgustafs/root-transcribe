"""Data access for recording workspaces and memberships."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError, NotFoundError
from app.models.user import User
from app.models.workspace import (
    Workspace,
    WorkspaceMember,
)

logger = logging.getLogger("app.crud.workspace")


# Load what WorkspacePublic.from_orm_workspace reads (members).
_LOAD = (selectinload(Workspace.members),)
_SETTINGS_FIELDS = {"description", "visibility"}


async def list_for_user(session: AsyncSession, *, user: User) -> Sequence[Workspace]:
    """Workspaces the user may open: owned, a member of, or ``shared`` (org-wide)."""
    member_sq = select(WorkspaceMember.workspace_id).where(
        WorkspaceMember.user_id == user.id
    )
    result = await session.execute(
        select(Workspace)
        .where(
            or_(
                Workspace.created_by_id == user.id,
                Workspace.visibility == "shared",
                Workspace.id.in_(member_sq),
            )
        )
        .options(*_LOAD)
        .order_by(Workspace.created_at.desc())
    )
    return result.scalars().all()


async def get(session: AsyncSession, *, workspace_id: uuid.UUID) -> Workspace | None:
    """Return a workspace (with members) by id, or ``None``."""
    result = await session.execute(
        select(Workspace).options(*_LOAD).where(Workspace.id == workspace_id)
    )
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    created_by: User,
    visibility: str = "private",
) -> Workspace:
    """Create a workspace with its store set; 409 if the owner reuses a name.

    The creator is the implicit **owner** (``created_by_id``). ``visibility`` is
    ``private`` (owner + members) or ``shared`` (any authenticated user may open it).
    Name uniqueness is scoped to the owner — another user's (possibly private)
    workspace must not block, or reveal, a name.
    """
    existing = await session.execute(
        select(Workspace.id).where(
            Workspace.created_by_id == created_by.id,
            Workspace.name == name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError(f"You already have a workspace named {name!r}")
    workspace = Workspace(
        name=name,
        description=description,
        created_by_id=created_by.id,
        visibility=visibility,
    )
    session.add(workspace)
    await session.commit()
    return await get(session, workspace_id=workspace.id)  # type: ignore[return-value]


# Fields patchable via update_settings; ``name`` is handled separately (uniqueness).


async def delete_workspace(session: AsyncSession, *, workspace: Workspace) -> None:
    """Delete a workspace and its dependent rows (cascade)."""
    await session.delete(workspace)
    await session.commit()


# --- Membership -------------------------------------------------------------


async def get_membership(
    session: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceMember | None:
    """The caller's membership row in a workspace, or ``None``."""
    result = await session.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def list_members(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> list[tuple[WorkspaceMember, str | None]]:
    """Members of a workspace paired with each user's email, oldest first.

    The **implicit owner** (the creator, who has no ``workspace_member`` row) is
    included as a synthetic ``owner`` entry so the roster and @mention typeahead
    don't silently omit them.
    """
    result = await session.execute(
        select(WorkspaceMember, User.email)
        .join(User, User.id == WorkspaceMember.user_id, isouter=True)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.created_at)
    )
    rows = list(result.all())
    workspace = await get(session, workspace_id=workspace_id)
    if (
        workspace is not None
        and workspace.created_by_id is not None
        and not any(m.user_id == workspace.created_by_id for m, _ in rows)
    ):
        creator = await session.get(User, workspace.created_by_id)
        synthetic = WorkspaceMember(
            id=workspace.created_by_id,  # stable, deterministic
            workspace_id=workspace_id,
            user_id=workspace.created_by_id,
            role="owner",
            created_at=workspace.created_at,
        )
        rows.insert(0, (synthetic, creator.email if creator else None))
    return rows


async def add_member(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> WorkspaceMember:
    """Add a member (or update their role if already present) — idempotent."""
    existing = await get_membership(session, workspace_id=workspace_id, user_id=user_id)
    if existing is not None:
        existing.role = role
        await session.commit()
        await session.refresh(existing)
        return existing
    member = WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role)
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


async def set_member_role(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> WorkspaceMember:
    """Change a member's role (404 if they aren't a member)."""
    member = await get_membership(session, workspace_id=workspace_id, user_id=user_id)
    if member is None:
        raise NotFoundError("Workspace member not found")
    member.role = role
    await session.commit()
    await session.refresh(member)
    return member


async def remove_member(
    session: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Remove a member (404 if they aren't one)."""
    member = await get_membership(session, workspace_id=workspace_id, user_id=user_id)
    if member is None:
        raise NotFoundError("Workspace member not found")
    await session.delete(member)
    await session.commit()
