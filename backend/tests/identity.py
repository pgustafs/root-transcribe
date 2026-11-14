"""RSA credentials for tests; never shipped as an application login path."""

from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.models.user import User

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def create_access_token(user: User) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user.id),
            "iss": settings.KEYCLOAK_ISSUER,
            "aud": settings.KEYCLOAK_AUDIENCE,
            "azp": settings.KEYCLOAK_CLIENT_ID,
            "typ": "Bearer",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "email": user.email,
            "email_verified": True,
            "name": user.full_name,
            "realm_access": {"roles": [user.role.value]},
        },
        SIGNING_KEY,
        algorithm="RS256",
    )
