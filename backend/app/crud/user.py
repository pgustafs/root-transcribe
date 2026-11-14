"""User repository: async CRUD operations against the database.

This layer isolates SQLAlchemy queries from the API layer. Functions take an
``AsyncSession`` and operate on/return ORM models. Business-rule violations raise
errors from :mod:`app.core.exceptions`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AuthenticationError, NotFoundError
from app.models.user import Role, User
from app.schemas.identity import IdentityClaims
from app.schemas.user import UserAccessUpdate, UserProfileUpdate


async def get(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Return a user by id, or ``None`` if not found."""
    return await session.get(User, user_id)


async def get_or_404(session: AsyncSession, user_id: uuid.UUID) -> User:
    """Return a user by id, raising :class:`NotFoundError` if missing."""
    user = await get(session, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return user


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """Return a user by email, or ``None`` if not found."""
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def list_users(
    session: AsyncSession,
    *,
    skip: int = 0,
    limit: int = 100,
) -> Sequence[User]:
    """Return a page of users ordered by creation time."""
    result = await session.execute(
        select(User).order_by(User.created_at).offset(skip).limit(limit)
    )
    return result.scalars().all()


async def count_active_admins(session: AsyncSession) -> int:
    """Number of active admin accounts (guards against admin lockout)."""
    result = await session.execute(
        select(func.count())
        .select_from(User)
        .where(User.role == Role.ADMIN, User.is_active.is_(True))
    )
    return int(result.scalar_one())


async def resolve_identity(session: AsyncSession, claims: IdentityClaims) -> User:
    """Upsert by trusted issuer/subject; never claim an account by email.

    The unique identity index arbitrates concurrent first requests. A savepoint
    contains a losing insert so unrelated work in the outer transaction survives.
    """
    query = select(User).where(
        User.oidc_issuer == settings.KEYCLOAK_ISSUER,
        User.oidc_subject == claims.sub,
    )
    user = await session.scalar(query)
    if user is None:
        try:
            async with session.begin_nested():
                user = User(
                    oidc_issuer=settings.KEYCLOAK_ISSUER,
                    oidc_subject=claims.sub,
                    email=str(claims.email),
                    full_name=claims.name,
                    hashed_password="",
                    role=claims.role,
                    is_active=True,
                )
                session.add(user)
                await session.flush()
        except IntegrityError:
            user = await session.scalar(query)
            if user is None:
                raise AuthenticationError("Identity requires operator review") from None
    if not user.is_active:
        raise AuthenticationError("Inactive user")
    if (user.email, user.full_name, user.role) != (
        str(claims.email),
        claims.name,
        claims.role,
    ):
        try:
            async with session.begin_nested():
                user.email = str(claims.email)
                user.full_name = claims.name
                user.role = claims.role
                await session.flush()
        except IntegrityError:
            raise AuthenticationError("Identity requires operator review") from None
    await session.commit()
    return user


async def update_profile(
    session: AsyncSession, user: User, payload: UserProfileUpdate
) -> User:
    if "avatar_url" in payload.model_fields_set:
        user.avatar_url = payload.avatar_url
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def update_access(
    session: AsyncSession, user: User, payload: UserAccessUpdate
) -> User:
    user.is_active = payload.is_active
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user
