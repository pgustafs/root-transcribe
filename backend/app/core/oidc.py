"""Bounded, cached verification of Keycloak RS256 access tokens."""

from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from app.core.config import settings


class TokenVerifier:
    def __init__(self) -> None:
        # Cache the set, not individual keys indefinitely: revoked keys age out.
        self._jwks = jwt.PyJWKClient(
            settings.KEYCLOAK_JWKS_URL,
            lifespan=300,
            timeout=settings.KEYCLOAK_JWKS_TIMEOUT_SECONDS,
        )

    def _signing_key(self, token: str) -> RSAPublicKey:
        key = self._jwks.get_signing_key_from_jwt(token).key
        if not isinstance(key, RSAPublicKey):
            raise jwt.InvalidKeyError("Expected RSA public key")
        return key

    def verify(self, token: str, *, verify_exp: bool = True) -> dict[str, Any]:
        if len(token) > 32768:
            raise jwt.InvalidTokenError("Token too large")
        claims = jwt.decode(
            token,
            self._signing_key(token),
            algorithms=["RS256"],
            issuer=settings.KEYCLOAK_ISSUER,
            audience=settings.KEYCLOAK_AUDIENCE,
            leeway=30,
            options={
                "require": ["sub", "iss", "aud", "exp", "iat", "typ", "azp"],
                "verify_exp": verify_exp,
            },
        )
        if claims["typ"] != "Bearer" or claims["azp"] != settings.KEYCLOAK_CLIENT_ID:
            raise jwt.InvalidTokenError("Invalid access token")
        return claims


@lru_cache
def get_token_verifier() -> TokenVerifier:
    return TokenVerifier()
