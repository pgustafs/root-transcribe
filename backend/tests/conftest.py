"""Shared pytest fixtures.

The suite runs against an in-memory SQLite database (via ``aiosqlite``) so it needs
no external services. Because the application code uses SQLAlchemy 2.0's
dialect-agnostic types, the same models run on both SQLite (tests) and PostgreSQL
(production).

Fixtures provide:
* a fresh schema per test (``db_session``),
* an ``httpx.AsyncClient`` wired to the app with ``get_db`` overridden, and
* convenience user/admin factories and authenticated clients.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.config import settings as _settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.user import Role, User
from tests.identity import create_access_token

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# App-wide rate limiting is off by default across the suite so it never trips
# unrelated functional tests; the memory backend avoids any Redis dependency.
_settings.RATE_LIMIT_ENABLED = False
_settings.RATE_LIMIT_BACKEND = "memory"
# Pipeline tests replace the service boundary; no request reaches this address.
_settings.STT_BASE_URL = "http://stt.invalid/v1"


@pytest.fixture
async def engine():
    """Create a dedicated in-memory engine and schema for one test."""
    # StaticPool keeps a single shared connection so the in-memory database
    # persists across sessions within a test.
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(engine) -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` bound to the per-test engine.

    ``autoflush=False`` matches production (``app/db/session.py``): tests must
    exercise the same flush ordering the app runs with, or bulk-update-vs-ORM
    races stay invisible until deployment (#22 review round 3).
    """
    session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(engine, db_session) -> AsyncIterator[AsyncClient]:
    """Yield an HTTP client with the app's ``get_db`` overridden to the test DB."""
    session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _identity_user(
    session: AsyncSession, *, email: str, name: str, role: Role
) -> User:
    """Create a provisioned Keycloak identity without a local password flow."""
    import uuid

    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=email,
        full_name=name,
        role=role,
        is_active=True,
        hashed_password="",
        oidc_subject=str(user_id),
        oidc_issuer=_settings.KEYCLOAK_ISSUER,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture
async def normal_user(db_session: AsyncSession) -> User:
    return await _identity_user(
        db_session, email="user@example.com", name="Normal User", role=Role.USER
    )


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _identity_user(
        db_session, email="admin@example.com", name="Admin User", role=Role.ADMIN
    )


@pytest.fixture
async def power_user(db_session: AsyncSession) -> User:
    return await _identity_user(
        db_session, email="power@example.com", name="Power User", role=Role.POWER_USER
    )


def _auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user)}"}


@pytest.fixture
def user_token_headers(normal_user: User) -> dict[str, str]:
    """Bearer-token headers for the normal user."""
    return _auth_headers(normal_user)


@pytest.fixture
def admin_token_headers(admin_user: User) -> dict[str, str]:
    """Bearer-token headers for the admin user."""
    return _auth_headers(admin_user)


@pytest.fixture
def power_user_token_headers(power_user: User) -> dict[str, str]:
    """Bearer-token headers for the power user."""
    return _auth_headers(power_user)


@pytest.fixture(autouse=True)
def keycloak_test_keys(monkeypatch):
    """Only replace remote key discovery; exercise real signature/claim checks."""
    from app.core.oidc import get_token_verifier
    from tests.identity import SIGNING_KEY

    monkeypatch.setattr(
        get_token_verifier(), "_signing_key", lambda token: SIGNING_KEY.public_key()
    )
