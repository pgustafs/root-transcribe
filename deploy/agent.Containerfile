# Agent runner: the CUGA SDK behind a small HTTP service (see agent-runner/).
# Built only when an LLM endpoint is configured. CPU-only PyTorch on Linux.
FROM registry.access.redhat.com/ubi10:latest

RUN dnf -y install python3 python3-pip \
    && dnf clean all && rm -rf /var/cache/dnf

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/app/home \
    AGENT_RUNNER_WORK_DIR=/app/work

WORKDIR /app

COPY agent-runner/pyproject.toml agent-runner/uv.lock ./
# --no-cache: do not keep a second copy of every download in the image.
RUN uv venv "$VIRTUAL_ENV" --python python3.12 \
    && UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV" uv sync --frozen --no-dev --no-cache

COPY agent-runner/main.py ./main.py

RUN useradd -u 1001 -r -g 0 -d /app/home -s /sbin/nologin appuser \
    && mkdir -p /app/home /app/work \
    && chown -R 1001:0 /app/home /app/work \
    && chmod -R g=u /app/home /app/work

USER 1001

# Loopback only: other containers in the pod share it; nothing outside does.
CMD ["uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8011", "--log-level", "warning"]
