"""Celery signal handlers that record every task into the background-job ledger.

Connecting to Celery's signals means **no per-task code** is needed: enqueue,
start, finish, failure, retry and revoke are all captured for every task. Handlers
run in whichever process fires the signal — the API process (``after_task_publish``
on ``.delay``/``.apply_async``) and the worker (the rest) — so they use the **sync**
engine (:func:`app.db.sync_session.session_scope`).

Handlers must never raise into Celery's machinery, so each is wrapped to swallow
and log its own errors. ``_summarize_args`` redacts/truncates payloads so secrets
and base64 file bytes never reach the table.
"""

from __future__ import annotations

import logging
from typing import Any

from celery.signals import (
    after_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_revoked,
)

from app.crud import background_job as job_crud
from app.db.sync_session import session_scope

logger = logging.getLogger("app.worker.signals")

# kwarg-name fragments whose values must never be stored.
_SECRET_HINTS = (
    "content_b64",
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "hotkey",
)
_MAX_VALUE = 120
_MAX_SUMMARY = 1000


def _summarize_value(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    text = repr(value)
    return text if len(text) <= _MAX_VALUE else text[:_MAX_VALUE] + "…"


def _summarize_args(args: Any, kwargs: Any) -> str:
    """A short, secret-safe one-line view of a task's args/kwargs."""
    parts: list[str] = [_summarize_value(a) for a in (args or ())]
    for key, value in (kwargs or {}).items():
        if any(hint in str(key).lower() for hint in _SECRET_HINTS):
            parts.append(f"{key}=<redacted>")
        else:
            parts.append(f"{key}={_summarize_value(value)}")
    return ", ".join(parts)[:_MAX_SUMMARY]


def _status_from_state(state: str | None) -> str:
    mapping = {"SUCCESS": "success", "FAILURE": "failure", "REVOKED": "revoked"}
    return mapping.get(str(state).upper(), str(state).lower() if state else "started")


@after_task_publish.connect
def _on_publish(
    sender: Any = None,
    headers: Any = None,
    body: Any = None,
    **extra: Any,
) -> None:
    """Record a freshly enqueued task (runs in the enqueuing process)."""
    try:
        headers = headers or {}
        task_id = headers.get("id")
        if not task_id:
            return
        # Protocol v2 body is (args, kwargs, embed).
        if isinstance(body, (list, tuple)) and len(body) >= 2:
            args, kwargs = body[0], body[1]
        else:
            args, kwargs = (), {}
        with session_scope() as session:
            job_crud.record_published(
                session,
                task_id=task_id,
                task_name=str(sender) if sender else headers.get("task"),
                request_id=headers.get("request_id"),
                triggered_by=headers.get("user"),
                queue=extra.get("routing_key"),
                args_summary=_summarize_args(args, kwargs),
            )
    except Exception:  # noqa: BLE001 - ledger writes must never break enqueue
        logger.exception("background-job: failed to record published task")


@task_prerun.connect
def _on_prerun(task_id: Any = None, task: Any = None, **extra: Any) -> None:
    """Mark a task as started (runs in the worker)."""
    try:
        if not task_id:
            return
        request = getattr(task, "request", None)
        with session_scope() as session:
            job_crud.record_started(
                session,
                task_id=str(task_id),
                task_name=getattr(task, "name", None),
                worker=getattr(request, "hostname", None),
                retries=getattr(request, "retries", 0) or 0,
            )
    except Exception:  # noqa: BLE001
        logger.exception("background-job: failed to record task start")


@task_postrun.connect
def _on_postrun(
    task_id: Any = None, state: Any = None, retval: Any = None, **extra: Any
) -> None:
    """Mark a task terminal with its runtime (runs in the worker)."""
    try:
        if not task_id:
            return
        status = _status_from_state(state)
        # Only summarize a successful return value; failures carry error+traceback.
        result = _summarize_value(retval) if status == "success" else None
        with session_scope() as session:
            job_crud.record_finished(
                session, task_id=str(task_id), status=status, result_summary=result
            )
    except Exception:  # noqa: BLE001
        logger.exception("background-job: failed to record task finish")


@task_failure.connect
def _on_failure(
    task_id: Any = None,
    exception: Any = None,
    einfo: Any = None,
    **extra: Any,
) -> None:
    """Attach the error + traceback to a failed task (runs in the worker)."""
    try:
        if not task_id:
            return
        with session_scope() as session:
            job_crud.record_failure(
                session,
                task_id=str(task_id),
                error=str(exception) if exception else None,
                traceback=str(einfo) if einfo else None,
            )
    except Exception:  # noqa: BLE001
        logger.exception("background-job: failed to record task failure")


@task_retry.connect
def _on_retry(request: Any = None, **extra: Any) -> None:
    try:
        task_id = getattr(request, "id", None)
        if not task_id:
            return
        with session_scope() as session:
            job_crud.record_retry(session, task_id=str(task_id))
    except Exception:  # noqa: BLE001
        logger.exception("background-job: failed to record task retry")


@task_revoked.connect
def _on_revoked(request: Any = None, **extra: Any) -> None:
    try:
        task_id = getattr(request, "id", None)
        if not task_id:
            return
        with session_scope() as session:
            job_crud.record_revoked(session, task_id=str(task_id))
    except Exception:  # noqa: BLE001
        logger.exception("background-job: failed to record task revoke")
