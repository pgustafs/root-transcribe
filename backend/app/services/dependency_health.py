"""Transcription availability checks based on Celery queue consumers.

Shares a cached worker snapshot between the public configuration signal
and the recording submission gate.
"""

from __future__ import annotations

import asyncio
import logging
import time

import anyio.to_thread
from fastapi import status

from app.core.config import settings
from app.core.exceptions import AppError
from app.worker import celery_app

logger = logging.getLogger("app.services.dependency_health")

# ---------------------------------------------------------------------------
# Shared worker-queue snapshot (issue #37 §1)
#
# ``active_queues()`` is keyed by Celery node name. Once every worker role has a
# unique node name (issue #36), a SINGLE broadcast returns every worker's reply
# reliably. We take exactly ONE such snapshot per cache interval and derive all
# queue readiness from it — the transcription availability signal and upload
# gate — so they can never disagree within one poll and
# we don't fan out several simultaneous broker broadcasts.
# ---------------------------------------------------------------------------

_QUEUE_SNAPSHOT_TTL_SECONDS = 10.0
_queue_snapshot_cache: tuple[float, frozenset[str] | None] | None = None
# Single-flight guard: concurrent availability checks could all observe the
# empty cache and each fire a broker broadcast. The lock collapses them onto ONE
# probe per interval — the "one internally consistent snapshot" #37 §1 requires.
_queue_snapshot_lock = asyncio.Lock()


def _snapshot_consumed_queue_names() -> frozenset[str] | None:
    """One ``active_queues()`` broadcast → the set of queue names some worker
    advertises. ``None`` means the broker was unreachable (callers then treat
    every queue as having no consumer). Blocking (kombu) — call in a thread.
    """
    try:
        replies = celery_app.control.inspect(timeout=1.0).active_queues() or {}
    except Exception:  # noqa: BLE001 — broker down ⇒ nothing consuming, by definition
        return None
    return frozenset(
        q["name"] for worker in replies.values() for q in worker if q.get("name")
    )


async def _consumed_queue_names() -> frozenset[str] | None:
    """The shared queue snapshot, cached for ``_QUEUE_SNAPSHOT_TTL_SECONDS``.

    Single-flight: concurrent configuration and upload requests collapse onto
    ONE broker broadcast per interval rather than each probing an empty cache.
    """
    global _queue_snapshot_cache

    def _fresh(entry: tuple[float, frozenset[str] | None] | None) -> bool:
        return (
            entry is not None
            and time.monotonic() - entry[0] < _QUEUE_SNAPSHOT_TTL_SECONDS
        )

    cached = _queue_snapshot_cache
    if _fresh(cached):
        return cached[1]
    async with _queue_snapshot_lock:
        # Re-check under the lock: a caller that raced ahead may have just
        # refreshed the snapshot, so we don't broadcast again.
        cached = _queue_snapshot_cache
        if not _fresh(cached):
            names = await anyio.to_thread.run_sync(_snapshot_consumed_queue_names)
            cached = (time.monotonic(), names)
            _queue_snapshot_cache = cached
        return cached[1]


def _reset_queue_snapshot_cache() -> None:
    """Drop the cached snapshot so the next read re-probes (tests; worker restart
    diagnostics)."""
    global _queue_snapshot_cache
    _queue_snapshot_cache = None


async def _queue_has_consumer(queue: str) -> bool:
    """Whether ``queue`` has a consumer per the shared snapshot. Degrades to
    False on broker error — this feeds the public /config signal, which must
    never 500 because Redis is down."""
    names = await _consumed_queue_names()
    return names is not None and queue in names


async def _transcription_consumer_cached() -> bool:
    return await _queue_has_consumer("transcription")


async def ensure_transcription_path() -> None:
    """Route dependency: refuse to accept meeting work with no processing path.

    A recording submitted while no worker consumes the ``transcription`` queue
    would sit ``pending`` until the reconciler eventually fails it — reject the
    submission up front with actionable guidance instead (issue #6).
    """
    if not settings.STT_BASE_URL.strip():
        raise AppError(
            "Speech-to-text is not configured; set STT_BASE_URL and retry.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not await _transcription_consumer_cached():
        raise AppError(
            "No worker is consuming the 'transcription' queue - start the "
            "stt-worker and retry.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


async def meetings_availability() -> str:
    """The frontend's service signal: available | degraded | unavailable.

    ``unavailable`` = processing paused or audio storage not configured;
    ``degraded`` = missing STT endpoint or transcription consumer. Existing
    recordings remain visible while the UI disables new submissions.
    """
    if not (settings.MEETINGS_ENABLED and settings.AUDIO_S3_ENDPOINT_URL):
        return "unavailable"
    if not settings.STT_BASE_URL.strip():
        return "degraded"
    return "available" if await _transcription_consumer_cached() else "degraded"


def notes_configured() -> bool:
    """Whether an LLM endpoint is configured for meeting notes."""
    return bool(settings.LLM_BASE_URL.strip())


def notes_availability() -> str:
    """The frontend's signal for meeting notes: available | unavailable."""
    return "available" if notes_configured() else "unavailable"


async def ensure_notes_path() -> None:
    """Route dependency: refuse to queue a note nobody could write."""
    if not notes_configured():
        raise AppError(
            "Meeting notes are not configured; set LLM_BASE_URL and retry.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
