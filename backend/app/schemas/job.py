"""Background-job (Celery) schemas for the admin Jobs monitor."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Lifecycle states recorded for a Celery task (kept in sync with the frontend's
# JobStatus and app.worker.signals).
JobStatus = Literal["pending", "started", "success", "failure", "retry", "revoked"]


class JobPublic(BaseModel):
    """One Celery task execution as shown on the Jobs page."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: str
    task_name: str
    status: JobStatus
    request_id: str | None = None
    triggered_by: str | None = None
    worker: str | None = None
    queue: str | None = None
    retries: int = 0
    args_summary: str | None = None
    result_summary: str | None = None
    error: str | None = None
    traceback: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    runtime_ms: int | None = None


class JobSummary(BaseModel):
    """Aggregate job counts driving the page's header cards and poll decision."""

    total: int = 0
    pending: int = 0
    started: int = 0
    success: int = 0
    failure: int = 0
    retry: int = 0
    revoked: int = 0
    # True while any job is pending, running or waiting for a retry.
    active: bool = False
