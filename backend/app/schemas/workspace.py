"""Workspace containers and membership for recordings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.workspace import Workspace

WorkspaceVisibility = Literal["private", "shared"]
WorkspaceRole = Literal["owner", "manager", "editor", "viewer"]


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    visibility: WorkspaceVisibility = "private"


class WorkspacePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str | None
    visibility: WorkspaceVisibility
    owner_id: uuid.UUID | None = None
    member_count: int = 0
    created_by_id: uuid.UUID | None
    created_at: datetime
    my_role: WorkspaceRole | None = None

    @classmethod
    def from_orm_workspace(
        cls, workspace: Workspace, *, my_role: WorkspaceRole | None = None
    ) -> WorkspacePublic:
        return cls.model_validate(workspace).model_copy(
            update={
                "my_role": my_role,
                "owner_id": workspace.created_by_id,
                "member_count": len(workspace.members),
            }
        )


class WorkspaceMemberCreate(BaseModel):
    """Add a user to a workspace with a role.

    Identify the user by ``user_id`` (admins, who have the directory picker) or
    by exact ``email`` (owners/editors, who don't) — exactly one of the two.
    """

    user_id: uuid.UUID | None = None
    email: str | None = Field(default=None, max_length=255)
    role: WorkspaceRole = "viewer"

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> WorkspaceMemberCreate:
        if (self.user_id is None) == (self.email is None):
            raise ValueError("provide exactly one of user_id or email")
        return self


class WorkspaceMemberUpdate(BaseModel):
    """Change a member's role."""

    role: WorkspaceRole


class WorkspaceMemberPublic(BaseModel):
    """A workspace member (with the user's email for display)."""

    id: uuid.UUID
    user_id: uuid.UUID
    email: str | None = None
    role: WorkspaceRole
    created_at: datetime
