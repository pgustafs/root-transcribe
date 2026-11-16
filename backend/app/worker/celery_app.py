"""Celery application configured from settings.

Redis is used as both broker and result backend. Tasks live in
:mod:`app.worker.tasks` and are auto-discovered. Run a worker with::

    celery -A app.worker.celery_app worker -l info

Periodic maintenance tasks are driven by Celery beat (``beat_schedule`` below).
Run exactly ONE beat process per deployment::

    celery -A app.worker.celery_app beat -l info -s /tmp/celerybeat-schedule
"""

from __future__ import annotations

from celery import Celery, Task
from celery.schedules import crontab
from celery.signals import setup_logging, worker_process_init

from app.core.config import settings
from app.core.logging import (
    _attach_loki_handler,
    configure_logging,
    request_id_ctx,
)
from app.worker import metrics_exporter  # noqa: F401 — registers worker_ready


class ContextTask(Task):
    """Base task that restores the originating request's correlation id.

    When a task is enqueued from a request, pass the active id along::

        transcribe_attempt.apply_async(
            args=[attempt_id], headers={"request_id": request_id_ctx.get()}
        )

    The worker then stamps every log record the task emits with that
    ``request_id``, so a flow that crosses from the API into a background task
    stays correlated in the logs.
    """

    def __call__(self, *args: object, **kwargs: object) -> object:
        request_id = (self.request.get("request_id") if self.request else None) or "-"
        token = request_id_ctx.set(request_id)
        try:
            return super().__call__(*args, **kwargs)
        finally:
            request_id_ctx.reset(token)


celery_app = Celery(
    "app",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.worker.tasks", "app.worker.cleanup_tasks"],
    task_cls=ContextTask,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Acknowledge tasks only after they complete so they survive worker crashes.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    # Bound every task's runtime (see config). Soft limit raises inside the task
    # for a clean failure; hard limit force-kills a task that ignores it.
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT_MINUTES * 60,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT_MINUTES * 60,
    # Meeting transcription (STT + pyannote diarization + speaker embeddings)
    # routes to a dedicated ``transcription`` queue consumed by the stt-worker
    # container (deploy/stt.Containerfile, the heavy optional ``stt`` deps).
    # Enrolling a voiceprint also needs the embedding model, so it shares the
    # queue. Run that worker with:
    #   celery -A app.worker.celery_app worker -Q transcription
    task_routes={
        "tasks.transcribe_attempt": {"queue": "transcription"},
        "tasks.enroll_voiceprint": {"queue": "transcription"},
        "tasks.create_meeting_clip": {"queue": "transcription"},
        "tasks.apply_meeting_redaction": {"queue": "transcription"},
    },
    beat_schedule={
        "cleanup-old-jobs": {
            "task": "tasks.cleanup_old_jobs",
            "schedule": crontab(minute=30, hour=3),
        },
        "cleanup-old-audio": {
            "task": "tasks.cleanup_old_audio",
            "schedule": crontab(minute=20, hour=4),
        },
        "process-audio-deletions": {
            "task": "tasks.process_audio_deletions",
            "schedule": 600.0,
        },
        "dispatch-task-outbox": {
            "task": "tasks.dispatch_task_outbox",
            "schedule": 30.0,
        },
        "reconcile-transcription-attempts": {
            "task": "tasks.reconcile_transcription_attempts",
            "schedule": 120.0,
        },
        "expire-upload-sessions": {
            "task": "tasks.expire_upload_sessions",
            "schedule": crontab(minute=25),
        },
    },
)


@setup_logging.connect
def _configure_celery_logging(**_kwargs: object) -> None:
    """Use the application's logging configuration inside Celery workers.

    This runs in the prefork **main** process, before the worker pool forks its
    children. We deliberately do *not* attach the Loki handler here: it starts a
    background shipper thread, and a thread holding its lock at ``fork`` time
    leaves that lock locked forever in the child, deadlocking it on the first log
    record (the task then never runs and its job is stuck ``pending``). Each
    forked child attaches its own Loki shipper post-fork — see below.
    """
    configure_logging(attach_loki=False)


@worker_process_init.connect
def _attach_loki_in_child(**_kwargs: object) -> None:
    """Start a per-child Loki shipper *after* fork (prefork-safe).

    The main process configured stdout logging (inherited via fork); here each
    worker child spins up its own shipper thread, so no child ever inherits a
    lock held by a thread that no longer exists.
    """
    if settings.LOKI_ENABLED:
        _attach_loki_handler()


# Importing the signals module registers the handlers that record every task into
# the background-job ledger. Done here (after the app is created) so the handlers
# load in both the worker and the API process (which imports tasks -> celery_app).
from app.worker import signals  # noqa: E402, F401
