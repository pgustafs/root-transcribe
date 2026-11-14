"""Outbox delivery — the after-commit fast path and the shared send (issue #6).

The durable intent is the :class:`app.models.task_outbox.TaskOutbox` row,
committed with the domain rows. Delivery is best-effort here: the fast path
keeps normal-case latency at zero, and a failure simply leaves the row
``pending`` for the ``tasks.dispatch_task_outbox`` beat to retry.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task_outbox import TaskOutbox

logger = logging.getLogger("app.services.task_outbox")


def deliver_outbox_row(row: TaskOutbox) -> None:
    """Send one outbox row to the broker (sync; raises on failure).

    The outbox id becomes the Celery task id, so a double send of the same row
    is recognizable broker-side; the consumer CAS is the real duplicate guard.
    """
    from app.worker.celery_app import celery_app

    celery_app.send_task(
        row.task_name,
        kwargs=dict(row.payload or {}),
        queue=row.queue,
        task_id=str(row.id),
        headers=dict(row.headers) if row.headers else None,
    )


async def try_dispatch(session: AsyncSession, row: TaskOutbox) -> bool:
    """After-commit fast path: attempt immediate delivery, mark ``sent``.

    Never raises — a broker outage must not fail the API request whose domain
    rows already committed; the dispatcher beat picks the row up within its
    interval.
    """
    try:
        await anyio.to_thread.run_sync(deliver_outbox_row, row)
    except Exception:  # noqa: BLE001 — the beat retries; request must succeed
        logger.warning(
            "outbox fast-path send failed; dispatcher will retry",
            extra={"outbox_id": str(row.id), "task": row.task_name},
        )
        return False
    row.status = "sent"
    row.sent_at = datetime.now(UTC)
    await session.commit()
    return True
