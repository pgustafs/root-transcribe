"""Validated identity attributes after cryptographic verification."""

from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.models.user import Role


class RealmAccess(BaseModel):
    roles: list[str] = Field(default_factory=list, max_length=1000)


class IdentityClaims(BaseModel):
    sub: str = Field(min_length=1, max_length=255)
    email: EmailStr = Field(max_length=255)
    email_verified: Literal[True]
    name: str | None = Field(default=None, max_length=255)
    realm_access: RealmAccess = Field(default_factory=RealmAccess)

    @property
    def role(self) -> Role:
        # The application has a single role. Order is explicit and stable.
        for role in (Role.ADMIN, Role.POWER_USER, Role.FEEDBACK_REVIEWER, Role.USER):
            if role.value in self.realm_access.roles:
                return role
        return Role.USER
