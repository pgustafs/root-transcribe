FROM registry.access.redhat.com/ubi10:latest

RUN dnf -y install python3 python3-pip \
    && dnf clean all && rm -rf /var/cache/dnf
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
COPY backend/app ./app
COPY backend/alembic.ini ./alembic.ini
# TORCH_BUILD=cpu installs the same locked versions, but PyTorch's CPU-only
# wheels and none of the NVIDIA libraries: for hosts without an NVIDIA GPU
# (Apple silicon), where they would only add download time and image size.
ARG TORCH_BUILD=locked
RUN uv venv "$VIRTUAL_ENV" --python python3.12 \
    && if [ "$TORCH_BUILD" = cpu ]; then \
        uv export --frozen --no-dev --extra stt --no-hashes --no-annotate --no-emit-project \
            | grep -vE '^(nvidia-|cuda-|triton==)' \
            | sed -E 's/^(torch|torchaudio)==([^ ]+)/\1==\2+cpu/' > /tmp/requirements.txt \
        && grep -q '^torch==.*+cpu' /tmp/requirements.txt \
        && uv pip install -r /tmp/requirements.txt \
            --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match \
        && uv pip install --no-deps . \
        && rm /tmp/requirements.txt; \
    else \
        UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV" uv sync --frozen --no-dev --no-editable --extra stt; \
    fi

RUN printf '%s\n' '#!/bin/sh' \
    'exec celery -A app.worker.celery_app inspect ping -d "stt@$HOSTNAME" --timeout "${1:-15}" >/dev/null 2>&1' \
    > /usr/local/bin/worker-ping \
    && chmod 0755 /usr/local/bin/worker-ping

RUN useradd -u 1001 -r -g 0 -d /app -s /sbin/nologin appuser \
    && mkdir -p /app/.cache/huggingface \
    && chown -R 1001:0 /app "$VIRTUAL_ENV" \
    && chmod -R g=u /app
COPY --chmod=0755 deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY deploy/wait_for_audio_store.py /usr/local/bin/wait_for_audio_store.py
USER 1001

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["celery", "-A", "app.worker.celery_app", "worker", \
            "--hostname=stt@%h", \
            "-Q", "transcription", "-l", "info", "--concurrency", "1"]
