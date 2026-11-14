"""Application configuration.

All runtime configuration is centralized here using ``pydantic-settings``. Values
are read from environment variables and, for local development, from a ``.env`` file
(see ``.env.example``). Import the singleton :data:`settings` everywhere instead of
reading ``os.environ`` directly.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, ClassVar, Literal

from pydantic import (
    AnyHttpUrl,
    BeforeValidator,
    Field,
    PostgresDsn,
    computed_field,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_cors(value: str | list[str]) -> list[str]:
    """Accept CORS origins as a comma-separated string or a real list."""
    if isinstance(value, str) and not value.startswith("["):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return value
    raise ValueError(f"Invalid CORS origins value: {value!r}")


CorsOrigins = Annotated[list[AnyHttpUrl] | list[str], BeforeValidator(_parse_cors)]


class Settings(BaseSettings):
    """Strongly-typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    PROJECT_NAME: str = "Root Transcribe"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    BACKEND_CORS_ORIGINS: CorsOrigins = []

    KEYCLOAK_INTERNAL_URL: str = ""
    KEYCLOAK_URL: str = "http://localhost:8185"
    KEYCLOAK_REALM: str = "root-transcribe"
    KEYCLOAK_CLIENT_ID: str = "root-transcribe"
    KEYCLOAK_AUDIENCE: str = "root-transcribe-api"
    KEYCLOAK_JWKS_TIMEOUT_SECONDS: float = Field(default=5, gt=0, le=30)

    @property
    def KEYCLOAK_ISSUER(self) -> str:
        return f"{self.KEYCLOAK_URL.rstrip('/')}/realms/{self.KEYCLOAK_REALM}"

    @property
    def KEYCLOAK_JWKS_URL(self) -> str:
        base = (self.KEYCLOAK_INTERNAL_URL or self.KEYCLOAK_URL).rstrip("/")
        return f"{base}/realms/{self.KEYCLOAK_REALM}/protocol/openid-connect/certs"

    SECRET_KEY: str = "changethis-please-generate-a-strong-random-secret"
    ALGORITHM: str = "HS256"

    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "app"
    POSTGRES_PASSWORD: str = "app"
    POSTGRES_DB: str = "app"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT_SECONDS: int = 10

    REDIS_URL: str = "redis://localhost:6379/0"
    JOBS_RETENTION_DAYS: int = 14
    CELERY_TASK_SOFT_TIME_LIMIT_MINUTES: int = 30
    CELERY_TASK_TIME_LIMIT_MINUTES: int = 35

    LOKI_ENABLED: bool = False
    LOKI_URL: str = "http://localhost:3100"  # base url; /loki/api/v1/push appended
    LOKI_SERVICE_NAME: str = "fastapi-backend"
    LOKI_BATCH_MAX_RECORDS: int = 100  # flush once this many records are buffered
    LOKI_FLUSH_INTERVAL_SECONDS: float = 1.0  # ...or after this long, whichever first
    LOKI_TIMEOUT_SECONDS: float = 5.0  # per-push HTTP timeout

    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_BACKEND: str = "redis"  # "redis" | "memory"
    RATE_LIMIT_USER_PER_MINUTE: int = 240
    RATE_LIMIT_IP_PER_MINUTE: int = 120
    RATE_LIMIT_EXEMPT_PATHS: list[str] = ["/health", "/metrics"]

    # Operational pause switch; transcription is the application's core feature.
    MEETINGS_ENABLED: bool = True
    STT_BASE_URL: str = ""
    STT_API_KEY: str = ""
    STT_MODEL: str = "whisper-large-v3-turbo"
    STT_TIMEOUT_SECONDS: int = 3600
    STT_MAX_UPLOAD_MB: int = 25
    DIARIZATION_MODEL: str = "pyannote/speaker-diarization-community-1"
    SPEAKER_EMBEDDING_MODEL: str = "pyannote/wespeaker-voxceleb-resnet34-LM"
    SPEAKER_MATCH_THRESHOLD: float = 0.55
    SPEAKER_MATCH_MARGIN: float = 0.08
    SPEAKER_MATCH_SUGGEST_THRESHOLD: float = 0.45
    SPEAKER_MATCH_MAX_TURNS: int = 5
    SPEAKER_MATCH_MIN_TURN_SEC: float = 2.0
    BIOMETRIC_ACTIVE_KEY_ID: str = "legacy"
    BIOMETRIC_KEYS: dict[str, str] = {}
    CONSENT_POLICY_VERSION: str = "2026-07"
    MEETING_REQUIRE_CONSENT: bool = True
    MEETING_DLP_AUTO: bool = True
    WORKER_METRICS_PORT: int = 9465
    VOICEPRINT_RETENTION_DAYS_DEFAULT: int = 365
    HF_TOKEN: str = ""

    AUDIO_S3_ENDPOINT_URL: str = ""
    AUDIO_S3_ACCESS_KEY: str = ""
    AUDIO_S3_SECRET_KEY: str = ""
    AUDIO_S3_BUCKET: str = "meeting-audio"
    AUDIO_S3_REGION: str = "us-east-1"
    AUDIO_S3_KEY_PREFIX: str = ""
    AUDIO_MAX_BYTES: int = 500 * 1024 * 1024
    AUDIO_RETENTION_DAYS: int = 90
    AUDIO_DELETE_MAX_ATTEMPTS: int = 8
    AUDIO_DELETE_RETRY_BASE_SECONDS: int = 300
    AUDIO_DELETE_STALE_CLAIM_SECONDS: int = 900
    MEETING_UPLOAD_PART_MB: int = 12
    MEETING_UPLOAD_SESSION_TTL_HOURS: int = 24
    MEETING_MAX_DURATION_MINUTES: int = 120

    OUTBOX_MAX_SEND_ATTEMPTS: int = 10
    OUTBOX_RETRY_BASE_SECONDS: int = 30
    MEETING_ATTEMPT_START_TIMEOUT_SECONDS: int = 600
    MEETING_ATTEMPT_STALE_SECONDS: int = 3600
    MEETING_ATTEMPT_MAX_ENQUEUES: int = 5
    OUTBOX_SENT_RETENTION_HOURS: int = 168
    OUTBOX_KEYED_RESEND_SECONDS: int = 3600

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        """Async DSN used by the FastAPI request path (asyncpg driver)."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_SERVER,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_DATABASE_URI_SYNC(self) -> str:
        """Sync DSN used by Celery workers and Alembic (psycopg driver)."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_SERVER,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )

    _DEFAULT_SECRETS: ClassVar[dict[str, str]] = {
        "SECRET_KEY": "changethis-please-generate-a-strong-random-secret",
    }

    def default_secrets(self) -> list[str]:
        """Names of security settings still set to their shipped placeholder."""
        enforced = dict(self._DEFAULT_SECRETS)
        return [
            name for name, default in enforced.items() if getattr(self, name) == default
        ]

    def enforce_secrets(self) -> None:
        """Refuse to run a non-local environment on placeholder secrets.

        Called at app startup. ``local`` logs a warning; ``staging`` and
        ``production`` raise so the container exits instead of serving traffic
        signed with a public default key.
        """
        offenders = self.default_secrets()
        if not offenders:
            return
        message = (
            "Default placeholder secrets in use: "
            + ", ".join(offenders)
            + " — generate real values (e.g. `openssl rand -hex 32`)."
        )
        if self.ENVIRONMENT == "local":
            logging.getLogger("app.core.config").warning(message)
            return
        raise RuntimeError(message)


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Using an LRU cache means the environment is parsed only once per process.
    Tests can clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()


settings = get_settings()
