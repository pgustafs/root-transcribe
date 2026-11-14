#!/bin/sh
# Wait for backing services, then migrate (API) or await migrations (workers).
set -eu
python - <<'PY'
import os
import socket
import time
from urllib.parse import urlsplit

deadline = time.monotonic() + 180
redis = urlsplit(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))
for host, port, label in (
    (os.environ.get('POSTGRES_SERVER', 'localhost'), int(os.environ.get('POSTGRES_PORT', '5432')), 'PostgreSQL'),
    (redis.hostname or 'localhost', redis.port or 6379, 'Redis'),
):
    print(f'Waiting for {label}...', flush=True)
    while True:
        try:
            with socket.create_connection((host, port), timeout=2):
                break
        except OSError:
            if time.monotonic() >= deadline:
                raise SystemExit(f'{label} did not become reachable within 180 seconds')
            time.sleep(2)
PY

if [ "${WAIT_FOR_AUDIO_STORE:-false}" = "true" ]; then
    python /usr/local/bin/wait_for_audio_store.py
fi

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    alembic upgrade head
elif [ "${WAIT_FOR_MIGRATIONS:-false}" = "true" ]; then
    python - <<'PY'
import time
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from app.core.config import settings

engine = create_engine(settings.SQLALCHEMY_DATABASE_URI_SYNC, connect_args={'connect_timeout': 3})
expected = set(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())
deadline = time.monotonic() + 180
print('Waiting for the API to finish database migrations...', flush=True)
while True:
    try:
        with engine.connect() as connection:
            if set(MigrationContext.configure(connection).get_current_heads()) == expected:
                break
    except SQLAlchemyError:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Database migrations did not finish within 180 seconds; check backend logs')
    time.sleep(2)
engine.dispose()
PY
fi
exec "$@"
