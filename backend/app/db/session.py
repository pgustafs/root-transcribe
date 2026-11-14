"""Async database engine and session management for the request path.

The FastAPI request path uses an asyncio engine backed by ``asyncpg``. The
:func:`get_db` dependency yields a session per request and guarantees cleanup.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# ``pool_pre_ping`` avoids handing out connections dropped by the server/proxy.
# Pool sizing is explicit (DB_POOL_* in config.py). A bounded checkout timeout
# prevents requests from waiting indefinitely when database connections are busy.
engine = create_async_engine(
    settings.SQLALCHEMY_DATABASE_URI,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Yield a database session, rolling back on error and always closing.

    Used as a FastAPI dependency with the default ``request`` scope, so the
    session is closed after the response has been sent.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# Reusable typed dependency alias (per team FastAPI conventions).
SessionDep = Annotated[AsyncSession, Depends(get_db)]
