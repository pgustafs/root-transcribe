"""Application exception hierarchy and FastAPI exception handlers.

Raising one of these domain errors from anywhere in the service produces a
consistent JSON error response without each call site needing to build an
``HTTPException``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.errors")


class AppError(Exception):
    """Base class for expected, handled application errors.

    :param message: Human-readable message returned to the client.
    :param status_code: HTTP status code for the response.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(AppError):
    """The requested resource does not exist."""

    status_code = status.HTTP_404_NOT_FOUND


class ConflictError(AppError):
    """The request conflicts with current state (e.g. duplicate unique field)."""

    status_code = status.HTTP_409_CONFLICT


class AuthenticationError(AppError):
    """Credentials are missing or invalid."""

    status_code = status.HTTP_401_UNAUTHORIZED


class PermissionDeniedError(AppError):
    """The authenticated user lacks permission for this action."""

    status_code = status.HTTP_403_FORBIDDEN


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers translating :class:`AppError` into JSON responses."""

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        # 5xx-class problems are worth a stack trace; client errors are not.
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.exception("Unhandled application error: %s", exc.message)
        else:
            logger.info("Application error (%s): %s", exc.status_code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message},
        )
