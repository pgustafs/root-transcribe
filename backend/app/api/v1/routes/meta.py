"""Public branding, identity and transcription availability."""

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.config import AppConfig
from app.services.dependency_health import meetings_availability

router = APIRouter(tags=["meta"])


@router.get("/config")
async def get_config() -> AppConfig:
    return AppConfig(
        project_name=settings.PROJECT_NAME,
        keycloak_url=settings.KEYCLOAK_URL,
        keycloak_realm=settings.KEYCLOAK_REALM,
        keycloak_client_id=settings.KEYCLOAK_CLIENT_ID,
        meetings_enabled=settings.MEETINGS_ENABLED
        and bool(settings.AUDIO_S3_ENDPOINT_URL),
        meetings_status=await meetings_availability(),
        meeting_consent_required=settings.MEETING_REQUIRE_CONSENT,
        audio_max_bytes=settings.AUDIO_MAX_BYTES,
        recording_max_minutes=settings.MEETING_MAX_DURATION_MINUTES,
    )
