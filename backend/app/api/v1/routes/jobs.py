"""Background jobs (Celery) monitor — admin-only.

Surfaces the durable job ledger (`background_jobs`, populated by the Celery signal
handlers in :mod:`app.worker.signals`) so admins can see what ran, what's running,
what failed, how long it took, and trace a job to the logs via its task id /
request id. Reads are read-only; an in-flight job can also be **cancelled**.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AdminDep
from app.core.exceptions import ConflictError, NotFoundError
from app.crud import background_job as job_crud
from app.db.session import SessionDep
from app.schemas.job import JobPublic, JobStatus, JobSummary
from app.worker.celery_app import celery_app

logger = logging.getLogger("app.api.jobs")

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Non-terminal states whose task can still be cancelled.
_CANCELLABLE = ("pending", "started", "retry")


@router.get("")
async def list_jobs(
    session: SessionDep,
    _admin: AdminDep,
    status: Annotated[JobStatus | None, Query()] = None,
    task_name: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=255)] = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[JobPublic]:
    """List background jobs, newest first (admin only).

    Optional filters: ``status``, ``task_name``, and ``q`` (matches task id or
    request id).
    """
    jobs = await job_crud.list_jobs(
        session, status=status, task_name=task_name, q=q, skip=skip, limit=limit
    )
    return [JobPublic.model_validate(j) for j in jobs]


@router.get("/summary")
async def jobs_summary(session: SessionDep, _admin: AdminDep) -> JobSummary:
    """Per-status job counts for the page header (admin only)."""
    return await job_crud.summary(session)


@router.post("/{task_id}/cancel")
async def cancel_job(session: SessionDep, admin: AdminDep, task_id: str) -> JobPublic:
    """Cancel an in-flight job (admin only).

    Revokes the Celery task — dropped from the queue if still ``pending``,
    terminated (``SIGTERM``) if already running — and marks the ledger row
    ``revoked``. Only ``pending``/``started``/``retry`` jobs can be cancelled;
    cancelling a job that already finished returns ``409``.
    """
    job = await job_crud.get_by_task_id(session, task_id=task_id)
    if job is None:
        raise NotFoundError("Job not found")
    if job.status not in _CANCELLABLE:
        raise ConflictError(f"Cannot cancel a job that is already {job.status}")
    # Broadcast the revoke to the workers; terminate a running task too.
    celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    updated = await job_crud.mark_revoked(session, task_id=task_id)
    logger.info(
        "background job cancelled",
        extra={
            "operation": "jobs.cancel",
            "task_id": task_id,
            "task_name": job.task_name,
            "cancelled_by": admin.email,
        },
    )
    # mark_revoked re-fetches the same row we just confirmed exists.
    assert updated is not None
    return JobPublic.model_validate(updated)
