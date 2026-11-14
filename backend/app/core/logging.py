"""Structured (JSON) request/response logging with correlation context.

Every log record is emitted as a single JSON object so it can be shipped to and
queried in a log aggregator. Two pieces of per-request context are woven into
*every* record automatically (via :class:`ContextFilter`), so a whole request flow
can be reconstructed by filtering on them:

* ``request_id`` — a per-request correlation id (UUID), also returned to the client
  in the ``X-Request-ID`` header and propagated to Celery tasks.
* ``user`` — the authenticated principal (set by ``get_current_user``), or ``-``
  for anonymous/unauthenticated requests.

To add breadcrumbs inside the service layer, wrap an outbound/expensive operation
with :func:`log_operation`; it emits a ``start`` then a ``finish``/``error`` record
carrying the operation name, duration, and any extra fields you pass.

Use ``logging.getLogger("app.<module>")`` throughout the codebase to inherit this
configuration.
"""

from __future__ import annotations

import atexit
import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.config import dictConfig
from typing import Any

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import settings

# Per-request context. Defaults apply to any record emitted outside a request
# (startup, Celery tasks that didn't inherit an id, etc.).
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
user_ctx: ContextVar[str] = ContextVar("user", default="-")

logger = logging.getLogger("app.access")

# LogRecord attributes that are part of the record itself rather than caller
# supplied ``extra=`` fields. Anything *not* in here is treated as structured
# context and merged into the JSON payload.
_RESERVED_RECORD_KEYS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class ContextFilter(logging.Filter):
    """Inject the active request id and user into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        record.user = user_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object.

    Standard fields (timestamp, level, logger, message, request_id, user) are
    always present; any ``extra=`` fields and exception/stack info are merged in,
    so service-layer breadcrumbs stay fully structured and queryable.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user": getattr(record, "user", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_KEYS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class LokiHandler(logging.Handler):
    """Ship structured log records to a Loki log store via its HTTP push API.

    Buffers the formatted JSON lines and flushes them on a background daemon
    thread — so the network round-trip never blocks the thread that emitted the
    log — either every ``flush_interval`` seconds or once ``batch_max_records`` are
    buffered, whichever comes first.

    Resilient by design: any error while pushing is swallowed (reported once to
    stderr) so an unreachable or slow Loki can never crash or stall the app. This
    handler is purely additive — stdout logging is unaffected.

    Labels are kept low-cardinality (``service``, ``level``, ``user``) so Grafana
    can offer a cheap per-user dropdown; everything else (``request_id``,
    ``operation``, ``path``, …) stays inside the JSON line and is queried with
    LogQL ``| json``.
    """

    def __init__(
        self,
        url: str,
        *,
        service: str,
        batch_max_records: int,
        flush_interval: float,
        timeout: float,
    ) -> None:
        super().__init__()
        self._push_url = url.rstrip("/") + "/loki/api/v1/push"
        self._service = service
        self._batch_max_records = batch_max_records
        self._flush_interval = flush_interval
        self._client = httpx.Client(timeout=timeout)
        self.setFormatter(JsonFormatter())
        # The handler runs on its own thread, so give it its own context filter
        # rather than relying on a filter attached to another handler.
        self.addFilter(ContextFilter())

        self._buffer: list[tuple[str, dict[str, str], str]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._reported_error = False
        self._thread = threading.Thread(
            target=self._run, name="loki-log-shipper", daemon=True
        )
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer one record (O(1), non-blocking) and nudge the flush thread."""
        try:
            line = self.format(record)
            labels = {
                "service": self._service,
                "level": record.levelname,
                "user": str(getattr(record, "user", "-")),
            }
            ts_ns = str(int(record.created * 1_000_000_000))
            with self._lock:
                self._buffer.append((ts_ns, labels, line))
                buffer_full = len(self._buffer) >= self._batch_max_records
            if buffer_full:
                self._wake.set()
        except Exception:  # pragma: no cover - emit must never raise
            self.handleError(record)

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(self._flush_interval)
            self._wake.clear()
            self._flush()

    def _flush(self) -> None:
        """Drain the buffer into Loki streams and push them. Never raises."""
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer
            self._buffer = []
        # Group lines into Loki streams keyed by their (low-cardinality) label set.
        streams: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
        for ts_ns, labels, line in batch:
            key = tuple(sorted(labels.items()))
            stream = streams.get(key)
            if stream is None:
                stream = {"stream": labels, "values": []}
                streams[key] = stream
            stream["values"].append([ts_ns, line])
        try:
            response = self._client.post(
                self._push_url, json={"streams": list(streams.values())}
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - shipping must never crash the app
            self._report_once(exc)

    def _report_once(self, exc: Exception) -> None:
        if not self._reported_error:
            self._reported_error = True
            print(
                f"[loki] log shipping to {self._push_url} failed ({exc!r}); "
                "further errors suppressed. stdout logging is unaffected.",
                file=sys.stderr,
            )

    def is_running(self) -> bool:
        """True while the flush thread is alive and not shutting down.

        ``logging.config.dictConfig`` calls ``logging.shutdown`` on every registered
        handler, which invokes :meth:`close` and stops our flush thread. Re-attaching
        such a closed handler would buffer records that never ship, so the attach
        helper uses this to recreate a fresh shipper instead.
        """
        return self._thread.is_alive() and not self._stopping.is_set()

    def close(self) -> None:
        """Stop the flush thread, push whatever remains, and release resources."""
        self._stopping.set()
        self._wake.set()
        self._thread.join(timeout=self._flush_interval + 1.0)
        self._flush()
        self._client.close()
        super().close()


# Single shared shipper for the process (the API and Celery workers each own one).
# ``configure_logging`` is called more than once; we attach the same instance
# rather than spawning a new thread every time.
_loki_handler: LokiHandler | None = None


def _attach_loki_handler() -> None:
    """Attach the singleton :class:`LokiHandler` to the root logger.

    ``dictConfig`` resets the root logger's handler list on every call, so this
    must run *after* it and re-attach the (already running) shipper instead of
    creating a second one.
    """
    global _loki_handler
    root = logging.getLogger()
    # dictConfig() (called by configure_logging itself, and again by uvicorn's own
    # log config) runs logging.shutdown() on every registered handler, which closes
    # our shipper and stops its flush thread. So a previously created handler may be
    # dead here; recreate it rather than re-attaching a handler that will silently
    # buffer records it never flushes.
    if _loki_handler is None or not _loki_handler.is_running():
        _loki_handler = LokiHandler(
            settings.LOKI_URL,
            service=settings.LOKI_SERVICE_NAME,
            batch_max_records=settings.LOKI_BATCH_MAX_RECORDS,
            flush_interval=settings.LOKI_FLUSH_INTERVAL_SECONDS,
            timeout=settings.LOKI_TIMEOUT_SECONDS,
        )
        atexit.register(_loki_handler.close)
    if _loki_handler not in root.handlers:
        root.addHandler(_loki_handler)


def configure_logging(*, attach_loki: bool = True) -> None:
    """Configure the root logging stack from application settings.

    Idempotent: safe to call multiple times (e.g. app startup and Celery worker
    startup).

    ``attach_loki`` controls whether the :class:`LokiHandler` (which starts a
    background shipper *thread*) is attached. The Celery **prefork main process**
    must pass ``False``: a thread started before the pool forks leaves its lock
    held in any child forked mid-flush, deadlocking that child on its first log
    record. The forked children re-attach Loki themselves (post-fork) via the
    ``worker_process_init`` signal — see ``app/worker/celery_app.py``.
    """
    log_level = settings.LOG_LEVEL.upper()
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "context": {"()": ContextFilter},
            },
            "formatters": {
                "json": {"()": JsonFormatter},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "filters": ["context"],
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "level": log_level,
                "handlers": ["console"],
            },
            "loggers": {
                # Quiet the access logger from uvicorn; our middleware handles it.
                "uvicorn.access": {"level": "WARNING", "propagate": False},
                "uvicorn.error": {"level": log_level},
                "sqlalchemy.engine": {"level": "WARNING"},
                # httpx logs an INFO line per request; with Loki shipping enabled
                # that includes the handler's own POSTs to /loki/api/v1/push,
                # which would loop back into Loki as noise. Quiet it.
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
            },
        }
    )
    # Optionally also ship logs to Loki. Done after dictConfig (which would
    # otherwise drop the handler) and gated by a flag so dev/tests are untouched.
    if attach_loki and settings.LOKI_ENABLED:
        _attach_loki_handler()


def _safe_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Rename keys that collide with reserved ``LogRecord`` attributes.

    ``logging`` raises ``KeyError`` if an ``extra`` key shadows a built-in record
    attribute (``filename``, ``name``, ``module``, ``msg``, …), so we suffix any such
    key with ``_`` — caller-supplied context can never crash logging.
    """
    return {
        (f"{key}_" if key in _RESERVED_RECORD_KEYS else key): value
        for key, value in fields.items()
    }


@contextmanager
def log_operation(
    op_logger: logging.Logger, operation: str, **fields: Any
) -> Iterator[None]:
    """Log the start, completion (with duration), or failure of an operation.

    Wrap a discrete unit of work — typically an outbound transcription or
    storage API call — so the request flow shows what was attempted, how long it took,
    and whether it failed, all stamped with the active ``request_id``/``user``::

        with log_operation(logger, "stt.transcribe", model=model):
            result = client.audio.transcriptions.create(...)

    ``fields`` become structured context on each emitted record. Exceptions are
    logged (with a stack trace) and re-raised unchanged. Field names that collide
    with reserved ``LogRecord`` attributes (e.g. ``filename``, ``name``, ``module``)
    are renamed so logging itself can never crash the call it is observing.
    """
    start = time.perf_counter()
    op_logger.debug(
        "%s started",
        operation,
        extra=_safe_extra({"operation": operation, "event": "start", **fields}),
    )
    try:
        yield
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        op_logger.exception(
            "%s failed after %.1fms",
            operation,
            elapsed_ms,
            extra=_safe_extra(
                {
                    "operation": operation,
                    "event": "error",
                    "duration_ms": round(elapsed_ms, 2),
                    **fields,
                }
            ),
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - start) * 1000
        op_logger.info(
            "%s ok in %.1fms",
            operation,
            elapsed_ms,
            extra=_safe_extra(
                {
                    "operation": operation,
                    "event": "finish",
                    "duration_ms": round(elapsed_ms, 2),
                    **fields,
                }
            ),
        )


class LoggingMiddleware(BaseHTTPMiddleware):
    """Log one structured record per request (start, then completed/failed)."""

    def __init__(self, app: ASGIApp, *, header_name: str = "X-Request-ID") -> None:
        super().__init__(app)
        self.header_name = header_name

    @staticmethod
    def _restore_user(request: Request) -> None:
        """Copy the authenticated principal from request.state into user_ctx.

        ``get_current_user`` stamps the principal onto ``request.state`` (the shared
        request scope). The endpoint runs in a child context, so its ``user_ctx``
        does not flow back to this middleware — but ``request.state`` does, letting
        the request.finish/error record (and the Loki ``user`` label) name the user.
        """
        principal = getattr(request.state, "user", None)
        if principal:
            user_ctx.set(principal)

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(self.header_name) or uuid.uuid4().hex
        req_token = request_id_ctx.set(request_id)
        # Reset user for this request's context; get_current_user fills it in later.
        user_token = user_ctx.set("-")
        client_ip = request.client.host if request.client else "unknown"
        method, path = request.method, request.url.path
        start = time.perf_counter()

        logger.info(
            "request started: %s %s from %s",
            method,
            path,
            client_ip,
            extra={
                "event": "request.start",
                "method": method,
                "path": path,
                "client_ip": client_ip,
            },
        )
        try:
            response = await call_next(request)
        except Exception:
            # call_next runs the endpoint in a child context, so a user_ctx set by
            # get_current_user does not propagate back here; recover it from the
            # shared request scope (request.state) for the finish/error record.
            self._restore_user(request)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request failed: %s %s from %s after %.2fms",
                method,
                path,
                client_ip,
                elapsed_ms,
                extra={
                    "event": "request.error",
                    "method": method,
                    "path": path,
                    "client_ip": client_ip,
                    "duration_ms": round(elapsed_ms, 2),
                },
            )
            user_ctx.reset(user_token)
            request_id_ctx.reset(req_token)
            raise

        self._restore_user(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers[self.header_name] = request_id
        logger.info(
            "request completed: %s %s -> %s in %.2fms",
            method,
            path,
            response.status_code,
            elapsed_ms,
            extra={
                "event": "request.finish",
                "method": method,
                "path": path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 2),
            },
        )
        user_ctx.reset(user_token)
        request_id_ctx.reset(req_token)
        return response
