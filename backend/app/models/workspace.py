"""Recording workspace containers and role-based memberships."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Workspace(Base):
    """A recording library with an owner, membership and shared settings."""

    __tablename__ = "workspace"
    # Names are unique per OWNER, not globally — otherwise any user's (even
    # private) workspace would block, and thereby reveal, the name for everyone.
    __table_args__ = (
        UniqueConstraint("created_by_id", "name", name="workspace_owner_name_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # The owner (creator). Kept nullable/SET NULL so deleting a user doesn't orphan
    # the workspace; policy treats the creator as an implicit owner.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Who may open the workspace page: "private" (owner + members) or "shared" (any
    # authenticated user). This gates the *page and its content*, never KB access.
    visibility: Mapped[str] = mapped_column(
        String(20), default="private", server_default="private", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WorkspaceMember(Base):
    """A user's membership in a workspace with a role (unique per (workspace, user)).

    ``role`` is one of ``owner`` / ``editor`` / ``viewer``. The workspace creator is an
    implicit owner (via ``Workspace.created_by_id``) and need not have a row here.
    Membership controls access to the recording workspace.
    """

    __tablename__ = "workspace_member"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="workspace_member_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(20), default="viewer", server_default="viewer", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members")
