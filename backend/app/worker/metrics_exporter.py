"""Prometheus exposure for Celery workers (issue #6, wave F1).

The API process serves ``/metrics`` itself, but Celery preforks — each child
records observations in its own process, invisible to a scrape of the parent.
The standard fix is prometheus_client's **multiprocess mode**: every process
writes its series to value files under ``PROMETHEUS_MULTIPROC_DIR`` and the
parent serves an aggregated view.

Opt-in by environment: when ``PROMETHEUS_MULTIPROC_DIR`` is set (the deploy
manifest points it at a directory inside the container's own /tmp — the value
files only need to be shared between the processes of ONE container, and
``app/core/metrics.py`` creates the directory before the first prometheus
import), the worker parent starts a plain HTTP metrics server on
``WORKER_METRICS_PORT``. Without the env var this module does nothing — dev
workers behave exactly as before.
"""

from __future__ import annotations

import logging
import os

from celery.signals import worker_ready

from app.core.config import settings

logger = logging.getLogger("app.worker.metrics")


@worker_ready.connect
def start_metrics_server(**_: object) -> None:
    """Serve aggregated multiprocess metrics from the worker parent."""
    mp_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not mp_dir:
        return
    try:
        from prometheus_client import CollectorRegistry, multiprocess, start_http_server

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        start_http_server(settings.WORKER_METRICS_PORT, registry=registry)
        logger.info(
            "worker metrics server on :%s (multiproc dir %s)",
            settings.WORKER_METRICS_PORT,
            mp_dir,
        )
    except Exception:  # noqa: BLE001 — metrics must never take the worker down
        logger.exception("failed to start the worker metrics server")
