"""User ORM model and role enumeration."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, Enum, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Role(enum.StrEnum):
    """Identity-provider roles persisted with each application profile.

    Admins manage accounts and governance; power users manage speaker profiles.
    The historical feedback_reviewer value remains valid for existing identities
    but grants no extra capability in this app.
    """

    ADMIN = "admin"
    POWER_USER = "power_user"
    FEEDBACK_REVIEWER = "feedback_reviewer"
    USER = "user"


class User(Base, TimestampMixin):
    """A registered application user."""

    __tablename__ = "users"

    __table_args__ = (
        Index("ix_users_oidc_identity", "oidc_issuer", "oidc_subject", unique=True),
    )

    # Preserve application IDs and all resource ownership during SSO cutover.
    oidc_issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    # Retained database column; Keycloak-provisioned profiles use an empty value.
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Profile picture as a data URL (e.g. "data:image/png;base64,..."). Kept small
    # by a client + server size limit; see app/schemas/user.py.
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", native_enum=False, length=20),
        default=Role.USER,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User id={self.id} email={self.email!r} role={self.role.value}>"
