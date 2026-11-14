# Root Transcribe backend

FastAPI provides the API, SQLAlchemy persists application state, and Celery runs
transcription and maintenance tasks. See the [architecture](../ARCHITECTURE.md)
for component boundaries and processing flows, and the [root README](../README.md)
for the complete local and Podman setup.

## Layout

| Path | Purpose |
| --- | --- |
| `app/api/` | Versioned routes, Keycloak authentication and authorization dependencies |
| `app/schemas/` | Validated request/response contracts |
| `app/models/` and `app/crud/` | Database schema and query/mutation helpers |
| `app/services/` | Audio storage, ASR client, diarization, alignment, speaker matching, exports and retention |
| `app/worker/` | Celery routes, scheduled tasks, durable processing and job metrics |
| `app/alembic/` | Ordered database migrations |
| `tests/` | Unit and API tests, using SQLite and mocked service boundaries |

The API uses asynchronous PostgreSQL sessions (`asyncpg`); workers use synchronous
sessions (`psycopg`). General tasks consume `celery`; transcription, enrollment,
clip creation and audio redaction consume `transcription`. Run exactly one beat
scheduler per deployment. The API and general worker share an image; the
transcription worker adds the heavy optional `stt` dependencies.

Speech recognition is a remote OpenAI-compatible service configured with
`STT_BASE_URL`, `STT_API_KEY` and `STT_MODEL`. Diarization and embeddings run in
the transcription worker. `requested_language` persists Auto/English/Swedish;
the ASR client's `language` hint is applied to the full recording and all slices.
The returned `language` is separate provider output. No local model is loaded
by the API, and languages do not select different providers automatically.

## Development and verification

Use Python 3.12+ and uv. From this directory:

```sh
cp .env.example .env
# Configure backing services as described in the root README.
uv sync --frozen --extra dev
uv run --no-sync alembic upgrade head
uv run --no-sync fastapi dev app/main.py
```

The API listens on port 8000. OpenAPI is at `/api/v1/openapi.json`; interactive
docs are at `/docs`. `/health` is a liveness check, `/health/ready` checks the
application database, and `/api/v1/config` reports configuration/queue availability.
None of these endpoints proves that a speech model can process audio successfully.

For a transcription worker in this environment, install `--extra stt` as well;
use the explicit worker/beat commands in the root README. The service commands
use `--no-sync` after installation so they do not remove another process's
optional dependencies from a shared development environment.

Run offline checks from this directory:

```sh
uv sync --frozen --extra dev
uv lock --check
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync alembic heads
uv run --no-sync pytest
```

The normal tests mock storage/model calls and use in-memory SQLite. Optional
FFmpeg validation tests require the decoder extra:

```sh
uv sync --frozen --extra dev --extra ffmpeg
REQUIRE_FFMPEG=1 uv run --no-sync pytest tests/unit/test_media_validation_ffmpeg.py
```

Run live inference and PostgreSQL checks against a configured deployment
separately. Do not infer model quality or production database behavior from
mocked or SQLite tests.

## Database upgrades

Run `alembic upgrade head` on new or existing application databases. The initial
revision `0001_transcription` creates the schema; `0002_recording_language` adds
the persisted language preference, defaulting existing recordings to Auto.
Applied migrations are immutable. Create a new revision for schema changes and
preserve the database, object store and encryption keys together during upgrades.

The schema-parity test walks the entire migration chain, compares the result
with ORM metadata and exercises downgrade. A separate test checks that the
language migration preserves previously detected language values.
