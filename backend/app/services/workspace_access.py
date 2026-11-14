"""Workspace access policy for recording libraries and their membership.

Owners control deletion, ownership and member roles. Managers can manage
settings and non-owner memberships; editors can change content. Viewers and
shared-workspace readers have read access. Recording-specific capabilities and
sensitivity restrictions are enforced separately by meeting_permissions.
"""

from __future__ import annotations

from app.core.exceptions import PermissionDeniedError
from app.models.user import Role, User
from app.models.workspace import Workspace, WorkspaceMember

# Roles allowed per capability tier.
_CONTENT_ROLES = ("owner", "manager", "editor")
_MANAGE_ROLES = ("owner", "manager")


def effective_role(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> str | None:
    """The caller's effective role: ``owner`` (admin or creator), the member ``role``,
    or ``None`` (they can only see it because it's ``shared``)."""
    if user.role == Role.ADMIN:
        return "owner"
    if workspace.created_by_id is not None and workspace.created_by_id == user.id:
        return "owner"
    if membership is not None:
        return membership.role
    return None


def can_read_workspace(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> bool:
    """May the user open the recording workspace?

    Any role suffices, as does ``shared`` visibility (no membership).
    """
    if effective_role(user=user, workspace=workspace, membership=membership):
        return True
    return workspace.visibility == "shared"


def can_edit_content(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> bool:
    """May the user change workspace content? Editor and up."""
    return (
        effective_role(user=user, workspace=workspace, membership=membership)
        in _CONTENT_ROLES
    )


def can_manage_settings(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> bool:
    """May the user change workspace metadata? Manager and up."""
    return (
        effective_role(user=user, workspace=workspace, membership=membership)
        in _MANAGE_ROLES
    )


def can_delete_workspace(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> bool:
    """May the user delete the workspace? Owner (creator, co-owner, or admin) only."""
    return (
        effective_role(user=user, workspace=workspace, membership=membership) == "owner"
    )


def ensure_can_read_workspace(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> None:
    if not can_read_workspace(user=user, workspace=workspace, membership=membership):
        raise PermissionDeniedError("You do not have access to this workspace")


def ensure_can_edit_content(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> None:
    if not can_edit_content(user=user, workspace=workspace, membership=membership):
        raise PermissionDeniedError("You cannot edit this workspace's content")


def ensure_can_manage_settings(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> None:
    if not can_manage_settings(user=user, workspace=workspace, membership=membership):
        raise PermissionDeniedError("Only a workspace manager or owner can do this")


def ensure_can_own_workspace(
    *, user: User, workspace: Workspace, membership: WorkspaceMember | None
) -> None:
    if not can_delete_workspace(user=user, workspace=workspace, membership=membership):
        raise PermissionDeniedError("Only the workspace owner can do this")
