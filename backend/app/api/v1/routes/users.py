"""User management routes.

* ``/users/me``          — the authenticated user reads/updates their own profile.
* ``/users`` (admin)     — profile listing and local suspension, restricted to admins.

Every route returns :class:`UserPublic`, which omits ``hashed_password``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AdminDep, CurrentUserDep
from app.core.exceptions import ConflictError
from app.crud import user as user_crud
from app.db.session import SessionDep
from app.models.user import Role
from app.schemas.user import UserAccessUpdate, UserProfileUpdate, UserPublic

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def read_current_user(current_user: CurrentUserDep) -> UserPublic:
    """Return the authenticated user's own profile."""
    return current_user


@router.patch("/me")
async def update_current_user(
    session: SessionDep,
    current_user: CurrentUserDep,
    payload: UserProfileUpdate,
) -> UserPublic:
    """Update the application avatar; identity and privilege fields are rejected."""
    return await user_crud.update_profile(session, current_user, payload)


# --- Admin-only endpoints --------------------------------------------------


@router.get("")
async def list_users(
    session: SessionDep,
    _admin: AdminDep,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[UserPublic]:
    """List users (admin only)."""
    users = await user_crud.list_users(session, skip=skip, limit=limit)
    return [UserPublic.model_validate(u) for u in users]


@router.patch("/{user_id}")
async def update_user(
    session: SessionDep,
    _admin: AdminDep,
    user_id: uuid.UUID,
    payload: UserAccessUpdate,
) -> UserPublic:
    """Suspend or reactivate an application account (admin only).

    Refuses (409) to deactivate the **last active admin** — that
    would lock everyone out of every admin surface, including this one.
    """
    user = await user_crud.get_or_404(session, user_id)
    removes_admin = (
        user.role == Role.ADMIN
        and user.is_active is True
        and payload.is_active is False
    )
    if removes_admin and await user_crud.count_active_admins(session) <= 1:
        raise ConflictError("Cannot deactivate the last active admin")
    updated = await user_crud.update_access(session, user, payload)
    return updated
