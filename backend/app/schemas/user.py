"""User schemas.

These models form the public contract for the users API. ``UserPublic`` is used as
the response model so that ``hashed_password`` can never be serialized back to a
client.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import Role

# A data-URL avatar (~256KB image → ~350KB base64); cap generously.
AVATAR_MAX_LEN = 700_000


class UserBase(BaseModel):
    """Fields shared across user representations."""

    email: EmailStr
    full_name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=AVATAR_MAX_LEN)


class UserPublic(UserBase):
    """Safe, client-facing representation of a user."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: Role
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserProfileUpdate(BaseModel):
    """Application-owned profile fields. Identity fields belong to Keycloak."""

    model_config = ConfigDict(extra="forbid")
    avatar_url: str | None = Field(default=None, max_length=AVATAR_MAX_LEN)


class UserAccessUpdate(BaseModel):
    """Local emergency suspension, independent of the identity provider."""

    model_config = ConfigDict(extra="forbid")
    is_active: bool
