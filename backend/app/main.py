"""FastAPI application factory with database readiness and HTTP metrics."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import LoggingMiddleware, configure_logging
from app.core.metrics import MetricsMiddleware, register_pool_gauges
from app.core.rate_limit import RateLimitMiddleware
from app.db.session import SessionDep, engine

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle.

    On startup, verify database connectivity (fail fast on misconfiguration).
    Schema creation is handled by Alembic migrations, not here. On shutdown,
    dispose the engine's connection pool cleanly.
    """
    configure_logging()
    logger.info("Starting %s (env=%s)", settings.PROJECT_NAME, settings.ENVIRONMENT)
    # Refuse to serve a deployed environment on placeholder secrets (local warns).
    settings.enforce_secrets()
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("Database connection verified")
    try:
        yield
    finally:
        from app.core.rate_limit import close_rate_limiter

        await close_rate_limiter()
        await engine.dispose()
        logger.info("Shutdown complete; database engine disposed")


class ReadinessResponse(BaseModel):
    """Readiness response for the application database."""

    status: Literal["ok", "unavailable"]
    postgres: Literal["ok", "unavailable"]


async def _postgres_reachable(session: AsyncSession) -> bool:
    """One round trip; any failure means the core dependency is down."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        # The class only: a probe fires every few seconds during an outage,
        # and a driver traceback can carry the DSN.
        logger.warning(
            "readiness: PostgreSQL is unreachable",
            extra={"error": type(exc).__name__},
        )
        return False
    return True


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    configure_logging()

    app = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        lifespan=lifespan,
        swagger_ui_init_oauth={
            "clientId": settings.KEYCLOAK_CLIENT_ID,
            "usePkceWithAuthorizationCodeGrant": True,
        },
    )

    if settings.BACKEND_CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(o) for o in settings.BACKEND_CORS_ORIGINS],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Order matters: add_middleware prepends, so the last added is outermost.
    # RateLimit is added before Metrics/Logging so those wrap it — a rejected
    # (429) request is still timed and logged like any other response.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(MetricsMiddleware)
    register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness probe used by the container/orchestrator (no I/O)."""
        return {"status": "ok"}

    @app.get(
        "/health/ready",
        tags=["health"],
        response_model=ReadinessResponse,
        responses={
            503: {
                "model": ReadinessResponse,
                "description": "A core dependency (PostgreSQL) is unreachable.",
            }
        },
    )
    async def readiness(session: SessionDep, response: Response) -> ReadinessResponse:
        """Return 503 while the application database is unavailable."""
        if not await _postgres_reachable(session):
            response.status_code = 503
            return ReadinessResponse(status="unavailable", postgres="unavailable")
        return ReadinessResponse(
            status="ok",
            postgres="ok",
        )

    # Prometheus scrape endpoint (numbers only — no user data; see
    # ARCHITECTURE.md). An external Prometheus can scrape this endpoint.
    # A plain route (not an ASGI mount) so `/metrics` serves directly, without
    # the mount's 307 redirect to `/metrics/`.
    register_pool_gauges(engine)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
