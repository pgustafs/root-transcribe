"""Workspace routes for recording libraries, metadata and membership.

GET /workspaces is the admin-only management list; /workspaces/mine lists
workspaces the caller may open through ownership, membership or shared visibility.
Any authenticated user can create a private workspace. Shared visibility requires
an elevated role. Content, management and ownership operations follow the
four-role policy in app.services.workspace_access; recording-specific permissions
are enforced separately by the meeting routes.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep
from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.crud import meeting_recording as meeting_recording_crud
from app.crud import user as user_crud
from app.crud import workspace as workspace_crud
from app.db.session import SessionDep
from app.models.user import Role, User
from app.models.workspace import Workspace, WorkspaceMember
from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceMemberCreate,
    WorkspaceMemberPublic,
    WorkspaceMemberUpdate,
    WorkspacePublic,
)
from app.services import workspace_activity
from app.services.workspace_access import (
    effective_role,
    ensure_can_edit_content,
    ensure_can_manage_settings,
    ensure_can_own_workspace,
    ensure_can_read_workspace,
)

logger = logging.getLogger("app.api.workspaces")

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _is_privileged(user: User) -> bool:
    return user.role in (Role.ADMIN, Role.POWER_USER)


def _public(workspace: Workspace, *, user: User, membership: WorkspaceMember | None):
    role = effective_role(user=user, workspace=workspace, membership=membership)
    return WorkspacePublic.from_orm_workspace(workspace, my_role=role)  # type: ignore[arg-type]


# The actor/author/creator email fields carried by read-level public schemas.


async def _load(session: SessionDep, workspace_id: uuid.UUID) -> Workspace:
    workspace = await workspace_crud.get(session, workspace_id=workspace_id)
    if workspace is None:
        raise NotFoundError("Workspace not found")
    return workspace


async def _load_authorized(
    session: SessionDep, user: User, workspace_id: uuid.UUID, *, level: str
) -> tuple[Workspace, WorkspaceMember | None]:
    """Load a workspace + caller's membership; enforce the required capability.

    Levels map to the four-role policy (:mod:`app.services.workspace_access`):
    ``read`` (viewer+/shared), ``content`` (editor+: recording content),
    ``manage`` (manager+: settings and members), ``own`` (owner:
    delete, ownership transfer, owner-role grants).
    """
    workspace = await _load(session, workspace_id)
    membership = await workspace_crud.get_membership(
        session, workspace_id=workspace.id, user_id=user.id
    )
    kwargs = {"user": user, "workspace": workspace, "membership": membership}
    if level == "read":
        ensure_can_read_workspace(**kwargs)
    elif level == "content":
        ensure_can_edit_content(**kwargs)
    elif level == "manage":
        ensure_can_manage_settings(**kwargs)
    else:
        ensure_can_own_workspace(**kwargs)
    return workspace, membership


# --- Listing ----------------------------------------------------------------


@router.get("/mine")
async def list_my_workspaces(
    session: SessionDep, user: CurrentUserDep
) -> list[WorkspacePublic]:
    """Workspaces the caller may open (owned, a member of, or org-wide shared)."""
    workspaces = await workspace_crud.list_for_user(session, user=user)
    out: list[WorkspacePublic] = []
    for w in workspaces:
        membership = next((m for m in w.members if m.user_id == user.id), None)
        out.append(_public(w, user=user, membership=membership))
    return out


# --- CRUD -------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    session: SessionDep, user: CurrentUserDep, payload: WorkspaceCreate
) -> WorkspacePublic:
    """Create a workspace (self-serve; creator is owner). A ``shared`` (org-wide)
    workspace requires a privileged user; 409 on a duplicate name."""
    if payload.visibility == "shared" and not _is_privileged(user):
        raise PermissionDeniedError(
            "Only admins or power users can create a shared workspace"
        )
    workspace = await workspace_crud.create(
        session,
        name=payload.name,
        description=payload.description,
        created_by=user,
        visibility=payload.visibility,
    )
    return _public(workspace, user=user, membership=None)


@router.get("/{workspace_id}")
async def get_workspace(
    session: SessionDep, user: CurrentUserDep, workspace_id: uuid.UUID
) -> WorkspacePublic:
    """A workspace's detail (must be able to open it)."""
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="read"
    )
    return _public(workspace, user=user, membership=membership)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    session: SessionDep,
    user: CurrentUserDep,
    workspace_id: uuid.UUID,
) -> None:
    """Delete a workspace and its uploaded-sources store (owner or admin)."""
    workspace, _ = await _load_authorized(session, user, workspace_id, level="own")
    # Legal holds trump EVERY purge path (issue #6) — including deleting the
    # whole workspace. The check runs UNCONDITIONALLY (review round 2:
    # gating it on MEETINGS_ENABLED meant disabling the feature disabled
    # hold enforcement over existing evidence) and covers every recording,
    # including those whose raw audio was already purged.
    from app.crud import meeting_governance as meeting_governance_crud

    if await meeting_governance_crud.workspace_has_active_hold(
        session, workspace_id=workspace.id
    ):
        raise ConflictError(
            "legal_hold_active: this workspace contains recordings on "
            "legal hold and cannot be deleted"
        )
    # Tombstone recording and clip audio for durable deletion
    # (issue #6): the row CASCADE alone would orphan the blobs in the object
    # store forever, and inline best-effort deletes used to swallow failures.
    if settings.AUDIO_S3_ENDPOINT_URL:
        from app.crud import audio_object as audio_object_crud

        for (
            rec_id,
            audio_key,
        ) in await meeting_recording_crud.list_audio_keys_for_workspace(
            session, workspace_id=workspace.id
        ):
            obj = await audio_object_crud.ensure_for_key(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=audio_key,
                kind="meeting_recording",
                ref_id=rec_id,
                workspace_id=workspace.id,
            )
            await audio_object_crud.request_delete(
                session, obj, reason="workspace_delete", actor=user
            )
        from app.models.meeting_clip import MeetingClip

        clip_rows = (
            await session.execute(
                select(MeetingClip.id, MeetingClip.audio_key).where(
                    MeetingClip.workspace_id == workspace.id,
                    MeetingClip.audio_key.is_not(None),
                )
            )
        ).all()
        for clip_id, clip_key in clip_rows:
            obj = await audio_object_crud.ensure_for_key(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=clip_key,
                kind="clip",
                ref_id=clip_id,
                workspace_id=workspace.id,
            )
            await audio_object_crud.request_delete(
                session, obj, reason="workspace_delete", actor=user
            )
    await workspace_crud.delete_workspace(session, workspace=workspace)


# --- Sources (uploaded documents in the workspace's own store) --------------


# --- Members ----------------------------------------------------------------


@router.get("/{workspace_id}/members")
async def list_members(
    session: SessionDep, user: CurrentUserDep, workspace_id: uuid.UUID
) -> list[WorkspaceMemberPublic]:
    """List a workspace's members (any member/viewer may see them).

    Member **emails** are only exposed to editors and up — viewers (and
    shared-visibility readers) see the roster without addresses.
    """
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="read"
    )
    role = effective_role(user=user, workspace=workspace, membership=membership)
    show_emails = role in ("owner", "manager", "editor")
    rows = await workspace_crud.list_members(session, workspace_id=workspace_id)
    return [
        WorkspaceMemberPublic(
            id=m.id,
            user_id=m.user_id,
            email=email if show_emails else None,
            role=m.role,  # type: ignore[arg-type]
            created_at=m.created_at,
        )
        for m, email in rows
    ]


@router.post("/{workspace_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    session: SessionDep,
    user: CurrentUserDep,
    workspace_id: uuid.UUID,
    payload: WorkspaceMemberCreate,
) -> WorkspaceMemberPublic:
    """Add a member (manager+) by user id (admin picker) or exact email (owners
    without directory access). Adding someone never grants them KB access.
    Granting the ``owner`` role is owner-only.
    """
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="manage"
    )
    if payload.role == "owner":
        ensure_can_own_workspace(user=user, workspace=workspace, membership=membership)
    if payload.user_id is not None:
        target = await user_crud.get(session, payload.user_id)
    else:
        target = await user_crud.get_by_email(session, payload.email or "")
    # One generic message: an inactive account behaves like an unknown one.
    if target is None or not target.is_active:
        raise NotFoundError("No active user with that id or email")
    member = await workspace_crud.add_member(
        session,
        workspace_id=workspace_id,
        user_id=target.id,
        role=payload.role,
    )
    await workspace_activity.record_activity(
        session,
        workspace_id=workspace_id,
        actor=user,
        event_type="member.added",
        payload={"member_email": target.email, "role": member.role},
    )
    return WorkspaceMemberPublic(
        id=member.id,
        user_id=member.user_id,
        email=target.email,
        role=member.role,  # type: ignore[arg-type]
        created_at=member.created_at,
    )


@router.patch("/{workspace_id}/members/{user_id}")
async def update_member_role(
    session: SessionDep,
    user: CurrentUserDep,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: WorkspaceMemberUpdate,
) -> WorkspaceMemberPublic:
    """Change a member's role (manager+). Granting or revoking the ``owner``
    member role is owner-only."""
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="manage"
    )
    target_membership = await workspace_crud.get_membership(
        session, workspace_id=workspace_id, user_id=user_id
    )
    if payload.role == "owner" or (
        target_membership is not None and target_membership.role == "owner"
    ):
        ensure_can_own_workspace(user=user, workspace=workspace, membership=membership)
    member = await workspace_crud.set_member_role(
        session, workspace_id=workspace_id, user_id=user_id, role=payload.role
    )
    target = await user_crud.get(session, user_id)
    await workspace_activity.record_activity(
        session,
        workspace_id=workspace_id,
        actor=user,
        event_type="member.role_changed",
        payload={
            "member_email": target.email if target else None,
            "role": member.role,
        },
    )
    return WorkspaceMemberPublic(
        id=member.id,
        user_id=member.user_id,
        email=target.email if target else None,
        role=member.role,  # type: ignore[arg-type]
        created_at=member.created_at,
    )


@router.delete(
    "/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_member(
    session: SessionDep,
    user: CurrentUserDep,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Remove a member (manager+). Removing a co-owner is owner-only."""
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="manage"
    )
    target_membership = await workspace_crud.get_membership(
        session, workspace_id=workspace_id, user_id=user_id
    )
    if target_membership is not None and target_membership.role == "owner":
        ensure_can_own_workspace(user=user, workspace=workspace, membership=membership)
    await workspace_crud.remove_member(
        session, workspace_id=workspace_id, user_id=user_id
    )
    target = await user_crud.get(session, user_id)
    await workspace_activity.record_activity(
        session,
        workspace_id=workspace_id,
        actor=user,
        event_type="member.removed",
        payload={"member_email": target.email if target else None},
    )


# --- Prompt library ---------------------------------------------------------


# --- Notes / artifacts ------------------------------------------------------


# --- Note comments --------------------------------------------------------------


# --- Export --------------------------------------------------------------------


# --- Saved source selections ---------------------------------------------------


# --- Health --------------------------------------------------------------------


# --- Search --------------------------------------------------------------------


# --- Activity feed ------------------------------------------------------------
