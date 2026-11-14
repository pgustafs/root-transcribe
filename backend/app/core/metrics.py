"""Prometheus HTTP, database-pool and transcription pipeline metrics."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

# Multiprocess mode (Celery workers): prometheus_client writes per-process
# value files under PROMETHEUS_MULTIPROC_DIR at import time and raises
# FileNotFoundError if the directory does not exist — the manifest only sets
# the env var, so create the directory BEFORE the first prometheus import.
if metrics_directory := os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
    os.makedirs(metrics_directory, exist_ok=True)

from prometheus_client import (  # noqa: E402 — must follow the makedirs above
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
)
from starlette.middleware.base import (  # noqa: E402
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402
from starlette.routing import Match  # noqa: E402


def _get_or_create(factory: Callable[[], Any], name: str) -> Any:
    """Return the already-registered collector ``name`` or create it.

    Metric constructors raise on duplicate registration; tests import/create the
    app repeatedly in one process, so creation must be idempotent.
    """
    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    return factory()


# HTTP request duration buckets, in seconds.
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, float("inf"))


# --- Meetings / transcription (issue #6) -----------------------------------

MEETING_ATTEMPTS = _get_or_create(
    lambda: Counter(
        "app_meeting_transcription_attempts_total",
        "Terminal transcription attempts by status (completed | failed | "
        "duplicate | cancelled). A rising failed rate is the pipeline alarm.",
        ["status"],
    ),
    "app_meeting_transcription_attempts_total",
)

MEETING_MEDIA_REJECTED = _get_or_create(
    lambda: Counter(
        "app_meeting_media_rejected_total",
        "Recordings rejected by the media-validation boundary, by stable error "
        "code (recording_invalid_container | recording_incomplete | "
        "recording_unsupported_codec | recording_limit_exceeded | "
        "recording_decode_timeout). Genuine media faults ONLY — infra/internal "
        "failures (transcription_service_unavailable/_internal_error) are "
        "deliberately excluded so this stays the bad-media alarm (issue #37). "
        "Low-cardinality: the code only — never a workspace/recording/user id.",
        ["code"],
    ),
    "app_meeting_media_rejected_total",
)

MEETING_CLIENT_EVENTS = _get_or_create(
    lambda: Counter(
        "app_meeting_client_events_total",
        "Recorder events by stable category (issue #37): preflight_empty | "
        "preflight_too_small | preflight_bad_header | assembly_incomplete | "
        "assembly_duplicate | upload_recovered | upload_recovery_failed — all "
        "reported by the BROWSER, because they happen where the server cannot "
        "see them — plus media_rejected, which the WORKER emits when it rejects "
        "media (a client-submitted verdict would be forgeable and replayed on "
        "every reload). A preflight_* spike means clients are PRODUCING bad "
        "media; correlate with the browser recorded in the meeting.client_event "
        "and meeting.media_rejected logs. Low-cardinality: the category only, "
        "never a workspace/recording/user id or a browser string.",
        ["event"],
    ),
    "app_meeting_client_events_total",
)

MEETING_STEP_SECONDS = _get_or_create(
    lambda: Histogram(
        "app_meeting_transcription_step_seconds",
        "Wall time per pipeline step (decode | diarize | transcribe | "
        "transcribe-turns | identify | persist).",
        ["step"],
        buckets=[1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600],
    ),
    "app_meeting_transcription_step_seconds",
)

MEETING_QUEUE_WAIT_SECONDS = _get_or_create(
    lambda: Histogram(
        "app_meeting_queue_wait_seconds",
        "Attempt creation to worker claim — queue wait. Sustained growth "
        "means the transcription worker is saturated (or gone).",
        buckets=[1, 5, 15, 60, 300, 900, 3600],
    ),
    "app_meeting_queue_wait_seconds",
)

MEETING_OUTBOX_PENDING = _get_or_create(
    lambda: Gauge(
        "app_meeting_outbox_pending",
        "Undelivered task_outbox rows (refreshed by the dispatcher beat). "
        "Non-zero for more than a couple of beats means the broker is down.",
        multiprocess_mode="livemax",
    ),
    "app_meeting_outbox_pending",
)

MEETING_OUTBOX_DEAD = _get_or_create(
    lambda: Counter(
        "app_meeting_outbox_dead_total",
        "Outbox rows dead-lettered after exhausting send retries.",
    ),
    "app_meeting_outbox_dead_total",
)

MEETING_DELETIONS = _get_or_create(
    lambda: Counter(
        "app_meeting_deletion_total",
        "Audio-object deletion sweep outcomes (confirmed | failed | dead_lettered).",
        ["result"],
    ),
    "app_meeting_deletion_total",
)

MEETING_DELETION_BACKLOG = _get_or_create(
    lambda: Gauge(
        "app_meeting_deletion_backlog",
        "audio_object rows awaiting deletion (delete_requested + deleting + "
        "delete_failed), refreshed by the deletion sweep.",
        multiprocess_mode="livemax",
    ),
    "app_meeting_deletion_backlog",
)

MEETING_SPEAKER_MATCHES = _get_or_create(
    lambda: Counter(
        "app_meeting_speaker_match_total",
        "Speaker-identification outcomes per diarized speaker (auto | review | none).",
        ["decision"],
    ),
    "app_meeting_speaker_match_total",
)

MEETING_UPLOADS = _get_or_create(
    lambda: Counter(
        "app_meeting_uploads_total",
        "Upload-session outcomes (completed | expired). A user-aborted session "
        "is not counted — only the absence of a completed reflects it. A rising "
        "expired rate means clients abandon mid-upload (network, tab close).",
        ["result"],
    ),
    "app_meeting_uploads_total",
)

MEETING_UPLOAD_PART_SECONDS = _get_or_create(
    lambda: Histogram(
        "app_meeting_upload_part_seconds",
        "Server time to receive+verify+store one upload part (~12 MB).",
        buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
    ),
    "app_meeting_upload_part_seconds",
)

MEETING_PLAYBACK_REQUESTS = _get_or_create(
    lambda: Counter(
        "app_meeting_playback_requests_total",
        "Audio proxy requests by kind (stream | download | clip) and "
        "HTTP status (200 | 206 | 416).",
        ["kind", "status"],
    ),
    "app_meeting_playback_requests_total",
)

MEETING_PLAYBACK_FIRST_BYTE_SECONDS = _get_or_create(
    lambda: Histogram(
        "app_meeting_playback_first_byte_seconds",
        "Auth + metadata + store round-trip before the first audio byte "
        "streams. The seek-latency the listener feels.",
        buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5],
    ),
    "app_meeting_playback_first_byte_seconds",
)

MEETING_REALTIME_FACTOR = _get_or_create(
    lambda: Histogram(
        "app_meeting_realtime_factor",
        "Transcription wall time / audio duration. Above ~1.0 the pipeline "
        "falls behind live recording pace.",
        buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8],
    ),
    "app_meeting_realtime_factor",
)


RATE_LIMITED = _get_or_create(
    lambda: Counter(
        "app_rate_limited_total",
        "Requests rejected by the app-wide rate limiter, by key scope.",
        ["scope"],  # user | ip
    ),
    "app_rate_limited_total",
)

RATE_LIMIT_BACKEND_ERRORS = _get_or_create(
    lambda: Counter(
        "app_rate_limit_backend_errors_total",
        "Rate-limiter backend errors (limiter failed open and allowed the request).",
    ),
    "app_rate_limit_backend_errors_total",
)

HTTP_REQUEST_SECONDS = _get_or_create(
    lambda: Histogram(
        "http_request_duration_seconds",
        "HTTP request latency by route template (excludes the SSE stream body).",
        ["method", "route", "status"],
        buckets=_HTTP_BUCKETS,
    ),
    "http_request_duration_seconds",
)


def register_pool_gauges(engine: Any) -> None:
    """Expose the async engine's connection-pool state as gauges.

    ``checked_out`` pinned at ``size + overflow`` is the pool-wait signal: new
    requests are queueing up to ``DB_POOL_TIMEOUT_SECONDS`` for a connection.
    """
    pool = engine.sync_engine.pool

    def _gauge(name: str, doc: str, fn: Callable[[], float]) -> None:
        g = _get_or_create(lambda: Gauge(name, doc), name)
        g.set_function(fn)

    _gauge(
        "db_pool_checked_out",
        "Connections currently checked out of the pool.",
        lambda: float(pool.checkedout()),
    )
    _gauge(
        "db_pool_size",
        "Base pool size (DB_POOL_SIZE).",
        lambda: float(pool.size()),
    )
    _gauge(
        "db_pool_overflow",
        "Overflow connections beyond the base size currently open.",
        lambda: float(max(pool.overflow(), 0)),
    )


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record per-request latency under the *route template* (bounded labels).

    Uses the matched route's ``path_format`` (e.g. ``/api/v1/users/{user_id}``)
    so label cardinality stays at the number of routes, not the number of ids.
    Unmatched paths are bucketed as ``(unmatched)``. For streaming responses,
    this measures time to response start rather than complete body transfer.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            HTTP_REQUEST_SECONDS.labels(
                method=request.method,
                route=self._route_template(request),
                status=status,
            ).observe(time.perf_counter() - start)

    @staticmethod
    def _route_template(request: Request) -> str:
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return getattr(route, "path_format", getattr(route, "path", "?"))
        return "(unmatched)"


# --- #31 propose-and-verify telemetry ---------------------------------------
#
# Every label below is a BOUNDED enum value drawn from server vocabulary:
# the pipeline's stages, the typed outcomes of a requirement, the
# deterministic gate reasons, and the turn's stop reasons. An unrecognized
# value collapses to "(unknown)" rather than opening a new series — the
# same escape hatch the tool metrics use. No question, query, document
# name, fact, quote or user identity is ever a label.

#: V19 splits the rejection side: a requirement the sources partly state,
#: with one condition unproven, is neither verified nor silent.
