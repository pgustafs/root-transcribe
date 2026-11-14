"""App-wide request rate limiting.

A generic per-request throttle applies to every route except the configured
exempt paths (``/health`` and ``/metrics`` by default).

**Algorithm — sliding-window counter approximation.** Each 60s wall-clock window
gets one integer counter; the rate for "the last minute" is estimated as the
current window's count plus the previous window's count weighted by how much of
the current window has *not* yet elapsed::

    estimate = current + previous * (1 - elapsed/60)

This is O(1) memory per key (two counters, not a request log) and smooths the
hard reset a naive fixed window would have at each minute boundary. It can admit
up to ~2x the limit only in a pathological burst straddling a boundary — fine
for abuse control.

**Two backends.** :class:`RedisRateLimiter` (default) shares state across
replicas via the Redis that Celery already uses; :class:`MemoryRateLimiter` is a
per-process fallback for tests and single-node dev. The middleware **fails open**
— if the backend raises (e.g. Redis is down) the request is allowed and the
error counted, so a cache outage never takes the API down.

**Keying.** Authenticated requests are keyed by the JWT ``sub`` (decoded for
identity only — all Keycloak claims verified, never used for auth here);
everything else falls back to the client IP. A forged/garbage token drops to the
IP bucket.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import jwt
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.metrics import RATE_LIMIT_BACKEND_ERRORS, RATE_LIMITED
from app.core.oidc import get_token_verifier

logger = logging.getLogger("app.core.rate_limit")

_WINDOW_SECONDS = 60
# Keep a window's counter alive across the current + previous window it can be
# read in, with headroom for clock skew.
_KEY_TTL_SECONDS = 2 * _WINDOW_SECONDS


def weighted_count(current: float, previous: float, elapsed: float) -> float:
    """Estimate the trailing-60s count from two adjacent fixed windows.

    ``elapsed`` is seconds into the current window; the previous window's count
    decays linearly as the current window fills.
    """
    fraction = 1.0 - min(max(elapsed, 0.0), _WINDOW_SECONDS) / _WINDOW_SECONDS
    return current + previous * fraction


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int  # seconds; 0 when allowed


def _retry_after(elapsed: float) -> int:
    """Seconds until the window rolls (a lower bound on when to retry)."""
    return max(1, math.ceil(_WINDOW_SECONDS - elapsed))


@runtime_checkable
class RateLimiter(Protocol):
    async def hit(self, key: str, *, per_minute: int) -> RateLimitResult:
        """Record one request against ``key`` and report whether it is admitted."""
        ...

    async def aclose(self) -> None: ...


class MemoryRateLimiter:
    """Per-process sliding-window limiter (tests / single-node fallback)."""

    def __init__(self, *, time_func: Callable[[], float] = time.time) -> None:
        self._now = time_func
        # key -> {window_index: count}
        self._counts: dict[str, dict[int, int]] = defaultdict(dict)
        self._last_sweep = 0

    def _sweep(self, window: int) -> None:
        """Drop keys whose windows are all stale (idle users/IPs).

        Per-key pruning in :meth:`hit` only runs when that key is hit again, so
        a key that goes quiet would otherwise be retained forever — unbounded
        growth by distinct IP. Swept at most once per window roll.
        """
        if window == self._last_sweep:
            return
        self._last_sweep = window
        stale = [
            key
            for key, windows in self._counts.items()
            if all(w < window - 1 for w in windows)
        ]
        for key in stale:
            del self._counts[key]

    async def hit(self, key: str, *, per_minute: int) -> RateLimitResult:
        if per_minute <= 0:  # 0/negative disables the limit
            return RateLimitResult(allowed=True, retry_after=0)
        now = self._now()
        window = int(now // _WINDOW_SECONDS)
        elapsed = now % _WINDOW_SECONDS
        self._sweep(window)
        windows = self._counts[key]
        for w in [w for w in windows if w < window - 1]:
            del windows[w]
        windows[window] = windows.get(window, 0) + 1
        estimate = weighted_count(windows[window], windows.get(window - 1, 0), elapsed)
        if estimate > per_minute:
            return RateLimitResult(allowed=False, retry_after=_retry_after(elapsed))
        return RateLimitResult(allowed=True, retry_after=0)

    async def aclose(self) -> None:
        self._counts.clear()


def _as_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return int(value.decode())
    return int(value)  # type: ignore[arg-type]


class RedisRateLimiter:
    """Sliding-window limiter backed by Redis (shared across replicas).

    One pipelined round-trip per request: INCR the current window, refresh its
    TTL, and read the previous window's count. Any client/transport error
    propagates so the middleware can fail open.
    """

    def __init__(self, *, client: object, time_func: Callable[[], float] = time.time):
        self._client = client
        self._now = time_func

    async def hit(self, key: str, *, per_minute: int) -> RateLimitResult:
        if per_minute <= 0:
            return RateLimitResult(allowed=True, retry_after=0)
        now = self._now()
        window = int(now // _WINDOW_SECONDS)
        elapsed = now % _WINDOW_SECONDS
        cur_key = f"{key}:{window}"
        prev_key = f"{key}:{window - 1}"
        pipe = self._client.pipeline(transaction=True)  # type: ignore[attr-defined]
        pipe.incr(cur_key)
        pipe.expire(cur_key, _KEY_TTL_SECONDS)
        pipe.get(prev_key)
        results = await pipe.execute()
        current = _as_int(results[0])
        previous = _as_int(results[2])
        estimate = weighted_count(current, previous, elapsed)
        if estimate > per_minute:
            return RateLimitResult(allowed=False, retry_after=_retry_after(elapsed))
        return RateLimitResult(allowed=True, retry_after=0)

    async def aclose(self) -> None:
        aclose = getattr(self._client, "aclose", None)
        if aclose is not None:
            await aclose()


# -- request identity ---------------------------------------------------------


def _user_id_from_auth(auth_header: str | None) -> str | None:
    """Return the JWT subject for keying, or ``None`` to fall back to IP.

    Keycloak signature and claims are checked before assigning a user bucket.
    Invalid credentials use the IP bucket; expiry is ignored for keying. The auth
    dependency independently verifies the token and local suspension.
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer ") :]
    try:
        claims = get_token_verifier().verify(token, verify_exp=False)
    except jwt.PyJWTError:
        return None
    sub = claims.get("sub")
    return str(sub) if sub else None


def request_identity(auth_header: str | None, client_ip: str) -> tuple[str, str, int]:
    """Map a request to its ``(scope, redis_key, per_minute_limit)``."""
    user_id = _user_id_from_auth(auth_header)
    if user_id is not None:
        return "user", f"rl:user:{user_id}", settings.RATE_LIMIT_USER_PER_MINUTE
    return "ip", f"rl:ip:{client_ip}", settings.RATE_LIMIT_IP_PER_MINUTE


# -- process-wide limiter -----------------------------------------------------

_limiter: RateLimiter | None = None


def _build_default() -> RateLimiter:
    if settings.RATE_LIMIT_BACKEND == "memory":
        return MemoryRateLimiter()
    import redis.asyncio as aioredis

    # Short socket timeouts: during a Redis outage every request would otherwise
    # wait out the default (multi-second) timeout before the middleware fails
    # open. One second bounds the worst-case added latency while staying far
    # above the healthy sub-millisecond round-trip.
    return RedisRateLimiter(
        client=aioredis.from_url(
            settings.REDIS_URL,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
    )


def get_rate_limiter() -> RateLimiter:
    """Return the process-wide limiter, building the configured backend once."""
    global _limiter
    if _limiter is None:
        _limiter = _build_default()
    return _limiter


async def close_rate_limiter() -> None:
    """Dispose the limiter's backend connection (lifespan shutdown)."""
    global _limiter
    if _limiter is not None:
        await _limiter.aclose()
        _limiter = None


# -- middleware ---------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honoring the pod's nginx ``X-Forwarded-For``.

    Behind the in-pod reverse proxy ``request.client`` is the proxy, so the
    first forwarded hop is the real client. Spoofable, but this only affects the
    anonymous IP bucket — an acceptable bound for a fallback key.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Throttle requests app-wide; fail open if the backend is unavailable."""

    def _is_exempt(self, path: str) -> bool:
        for prefix in settings.RATE_LIMIT_EXEMPT_PATHS:
            if path == prefix or path.startswith(prefix + "/"):
                return True
        return False

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not settings.RATE_LIMIT_ENABLED or self._is_exempt(request.url.path):
            return await call_next(request)

        scope, key, per_minute = await run_in_threadpool(
            request_identity, request.headers.get("authorization"), _client_ip(request)
        )
        try:
            result = await get_rate_limiter().hit(key, per_minute=per_minute)
        except Exception as exc:  # noqa: BLE001 - fail open on any backend error
            RATE_LIMIT_BACKEND_ERRORS.inc()
            logger.warning("Rate-limit backend error; failing open: %s", exc)
            return await call_next(request)

        if not result.allowed:
            RATE_LIMITED.labels(scope=scope).inc()
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded; slow down and try again."},
                headers={"Retry-After": str(result.retry_after)},
            )
        return await call_next(request)
