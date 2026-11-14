"""Tests for the extraction's public configuration and readiness contract."""

import importlib


async def test_config_only_advertises_retained_features(client, monkeypatch):
    meta = importlib.import_module("app.api.v1.routes.meta")

    async def available():
        return "available"

    monkeypatch.setattr(meta, "meetings_availability", available)
    response = await client.get("/api/v1/config")
    assert response.status_code == 200
    config = response.json()
    assert config["project_name"] == "Root Transcribe"
    assert config["keycloak_realm"] == "root-transcribe"
    assert "meetings_enabled" in config
    assert config["audio_max_bytes"] > 0
    assert config["recording_max_minutes"] > 0
    assert "custom_theme" not in config
    assert "users_open_registration" not in config
    assert not any(
        key.startswith(("rag", "chat", "podcast", "agentic")) for key in config
    )


async def test_missing_stt_endpoint_blocks_new_audio(
    client, monkeypatch, user_token_headers
):
    from app.core.config import settings
    from app.services import dependency_health

    monkeypatch.setattr(settings, "MEETINGS_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIO_S3_ENDPOINT_URL", "http://storage.example")
    monkeypatch.setattr(settings, "STT_BASE_URL", "")

    async def worker_ready():
        return True

    monkeypatch.setattr(
        dependency_health, "_transcription_consumer_cached", worker_ready
    )
    response = await client.get("/api/v1/config")
    assert response.json()["meetings_status"] == "degraded"
    workspace = await client.post(
        "/api/v1/workspaces", headers=user_token_headers, json={"name": "Interview"}
    )
    assert workspace.status_code == 201
    response = await client.post(
        f"/api/v1/workspaces/{workspace.json()['id']}/recordings",
        headers=user_token_headers,
        files={"file": ("interview.wav", b"audio", "audio/wav")},
    )
    assert response.status_code == 503
    assert "STT_BASE_URL" in response.json()["detail"]


async def test_retention_api_rejects_unimplemented_policies(
    client, monkeypatch, admin_token_headers
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "MEETINGS_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIO_S3_ENDPOINT_URL", "http://storage.example")
    workspace = await client.post(
        "/api/v1/workspaces", headers=admin_token_headers, json={"name": "Retention"}
    )
    url = f"/api/v1/workspaces/{workspace.json()['id']}/meetings/retention"
    for artifact in ("recap", "machine_transcript", "clip", "voiceprint"):
        response = await client.put(
            url,
            headers=admin_token_headers,
            json={"artifact": artifact, "retention_days": 1},
        )
        assert response.status_code == 400
    assert (await client.get(url, headers=admin_token_headers)).json() == []
    response = await client.put(
        url,
        headers=admin_token_headers,
        json={"artifact": "raw_audio", "retention_days": 7},
    )
    assert response.status_code == 200
    assert response.json()["retention_days"] == 7


async def test_readiness_reports_database_failure_without_chat_dependency(
    client, monkeypatch
):
    main = importlib.import_module("app.main")

    async def unreachable(session):
        return False

    monkeypatch.setattr(main, "_postgres_reachable", unreachable)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "postgres": "unavailable"}


async def test_user_updates_only_accept_application_fields(
    client, user_token_headers, admin_token_headers, normal_user
):
    avatar = "data:image/png;base64,dGVzdA=="
    response = await client.patch(
        "/api/v1/users/me", headers=user_token_headers, json={"avatar_url": avatar}
    )
    assert response.status_code == 200
    assert response.json()["avatar_url"] == avatar
    assert "hashed_password" not in response.json()
    response = await client.patch(
        "/api/v1/users/me", headers=user_token_headers, json={"role": "admin"}
    )
    assert response.status_code == 422
    response = await client.patch(
        f"/api/v1/users/{normal_user.id}",
        headers=admin_token_headers,
        json={"is_active": False, "password": "local-password"},
    )
    assert response.status_code == 422
    response = await client.patch(
        f"/api/v1/users/{normal_user.id}",
        headers=admin_token_headers,
        json={"is_active": False},
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    response = await client.get("/api/v1/users/me", headers=user_token_headers)
    assert response.status_code == 401


async def test_last_active_admin_cannot_be_suspended(
    client, admin_token_headers, admin_user
):
    response = await client.patch(
        f"/api/v1/users/{admin_user.id}",
        headers=admin_token_headers,
        json={"is_active": False},
    )
    assert response.status_code == 409


async def test_unused_consent_purposes_cannot_be_granted(
    client, user_token_headers, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "MEETINGS_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIO_S3_ENDPOINT_URL", "http://storage.example")
    for purpose in ("voice_synthesis", "model_improvement"):
        response = await client.post(
            "/api/v1/speakers/me/consents",
            headers=user_token_headers,
            json={"purpose": purpose},
        )
        assert response.status_code == 422


async def test_retrying_jobs_keep_the_monitor_active(
    client, db_session, admin_token_headers
):
    from app.models.background_job import BackgroundJob

    job = BackgroundJob(
        task_id="retrying", task_name="tasks.enroll_voiceprint", status="retry"
    )
    db_session.add(job)
    await db_session.commit()
    response = await client.get("/api/v1/jobs/summary", headers=admin_token_headers)
    assert response.status_code == 200
    assert response.json()["active"] is True
    job.status = "success"
    await db_session.commit()
    response = await client.get("/api/v1/jobs/summary", headers=admin_token_headers)
    assert response.json()["active"] is False
