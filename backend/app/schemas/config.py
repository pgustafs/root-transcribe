"""Public application configuration."""

from pydantic import BaseModel


class AppConfig(BaseModel):
    project_name: str
    keycloak_url: str
    keycloak_realm: str
    keycloak_client_id: str
    meetings_enabled: bool
    meetings_status: str
    meeting_consent_required: bool
    # available | unavailable: can meeting notes be created (is an LLM set)?
    notes_status: str = "unavailable"
    audio_max_bytes: int = 500 * 1024 * 1024
    recording_max_minutes: int = 120
