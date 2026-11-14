"""The Podman worker must be importable with an initially empty /tmp volume."""

import os
import subprocess
import sys


def test_worker_metrics_create_the_configured_directory(tmp_path):
    directory = tmp_path / "metrics"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.core.metrics import MEETING_ATTEMPTS; "
            "MEETING_ATTEMPTS.labels(status='completed').inc()",
        ],
        env={**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(directory)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert list(directory.glob("*.db"))
