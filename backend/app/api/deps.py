"""Shared API dependencies: authentication and authorization.

Following the team FastAPI conventions, dependencies are plain functions exposed as
reusable ``Annotated`` type aliases, and role checks use a *function factory*
(:func:`require_roles`) rather than a class dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2AuthorizationCodeBearer
from pydantic import ValidationError

from app.core.config import settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.logging import user_ctx
from app.core.oidc import TokenVerifier, get_token_verifier
from app.crud import user as user_crud
from app.db.session import SessionDep
from app.models.user import Role, User
from app.schemas.identity import IdentityClaims

oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=f"{settings.KEYCLOAK_ISSUER}/protocol/openid-connect/auth",
    tokenUrl=f"{settings.KEYCLOAK_ISSUER}/protocol/openid-connect/token",
)
TokenDep = Annotated[str, Depends(oauth2_scheme)]
VerifierDep = Annotated[TokenVerifier, Depends(get_token_verifier)]


async def get_current_user(
    request: Request, session: SessionDep, token: TokenDep, verifier: VerifierDep
) -> User:
    """Verify Keycloak identity, synchronize the profile, and enforce suspension."""
    try:
        claims = IdentityClaims.model_validate(
            await run_in_threadpool(verifier.verify, token)
        )
    except (jwt.PyJWTError, ValidationError) as exc:
        raise AuthenticationError("Could not validate credentials") from exc
    user = await user_crud.resolve_identity(session, claims)
    if not user.is_active:
        raise AuthenticationError("Inactive user")
    # Stamp the principal onto the logging context so every record emitted for
    # the rest of this request identifies who made it. Also stash it on
    # request.state: the endpoint runs in a child context whose user_ctx does not
    # propagate back to LoggingMiddleware, but the request scope (and thus
    # request.state) is shared, so the middleware can recover it for request.finish.
    principal = f"{user.id} [{user.role.value}]"
    user_ctx.set(principal)
    request.state.user = principal
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def require_roles(
    *roles: Role,
) -> Callable[[User], Coroutine[Any, Any, User]]:
    """Build a dependency that allows only users having one of ``roles``.

    Usage::

        AdminDep = Annotated[User, Depends(require_roles(Role.ADMIN))]
    """

    async def dependency(current_user: CurrentUserDep) -> User:
        if current_user.role not in roles:
            raise PermissionDeniedError("The user does not have enough privileges")
        return current_user

    return dependency


# Convenience alias for admin-only routes.
AdminDep = Annotated[User, Depends(require_roles(Role.ADMIN))]

# Speaker directory management admits admins and power users. Keep the
# frontend isPrivileged predicate in AuthProvider aligned with these roles.
PRIVILEGED_ROLES: tuple[Role, ...] = (Role.ADMIN, Role.POWER_USER)
PrivilegedDep = Annotated[User, Depends(require_roles(*PRIVILEGED_ROLES))]
