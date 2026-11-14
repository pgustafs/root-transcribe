"""Functional tests for the speaker-directory API (embedding worker mocked out)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

import app.services.audio_store as audio_store_mod
from app.core.config import settings
from app.models.user import User
from app.worker import tasks as worker_tasks

pytestmark = pytest.mark.functional

API = "/api/v1"


def _live_purposes(ledger: list[dict]) -> set[str]:
    """Effective granted purposes: latest ledger row per purpose wins (the
    ledger is append-only, so an old grant row is not a live grant)."""
    latest: dict[str, str] = {}
    for row in ledger:  # server returns newest-first
        latest.setdefault(row["purpose"], row["status"])
    return {p for p, status in latest.items() if status == "granted"}


class _FakeAudioStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.blobs[key] = data
        return key

    def get_sync(self, key: str) -> bytes:
        return self.blobs[key]

    async def get(self, key: str) -> bytes:
        return self.blobs[key]

    async def delete(self, key: str) -> None:
        self.blobs.pop(key, None)

    def delete_sync(self, key: str) -> None:
        self.blobs.pop(key, None)

    def stream_sync(self, key: str):
        yield self.blobs[key]


@pytest.fixture
def speakers_enabled(monkeypatch: pytest.MonkeyPatch) -> _FakeAudioStore:
    monkeypatch.setattr(settings, "MEETINGS_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIO_S3_ENDPOINT_URL", "http://s4:7480")
    store = _FakeAudioStore()
    monkeypatch.setattr(audio_store_mod, "_audio_store", store)
    monkeypatch.setattr(
        worker_tasks.enroll_voiceprint, "apply_async", lambda *a, **k: None
    )
    return store


async def test_disabled_returns_503(
    client: AsyncClient, user_token_headers: dict[str, str]
) -> None:
    # A PROCESSING endpoint is gated when the feature is off. The directory
    # read and DSAR/erasure endpoints are rights and stay reachable (issue #14
    # review), so probe enrollment, not the (now-ungated) directory list.
    resp = await client.post(
        f"{API}/speakers/me/voiceprints",
        headers=user_token_headers,
        files={"file": ("v.webm", b"voice", "audio/webm")},
        data={"consent_granted": "true"},
    )
    assert resp.status_code == 503
    # …and the ungated directory read is reachable even with the feature off.
    listed = await client.get(f"{API}/speakers", headers=user_token_headers)
    assert listed.status_code == 200


async def test_create_requires_privilege(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    admin_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
) -> None:
    # A normal user cannot create a directory entry.
    denied = await client.post(
        f"{API}/speakers",
        headers=user_token_headers,
        json={
            "display_name": "Alice",
            "consent_granted": True,
            "consent_evidence": "verbal consent in kickoff meeting",
        },
    )
    assert denied.status_code == 403

    created = await client.post(
        f"{API}/speakers",
        headers=admin_token_headers,
        json={
            "display_name": "Alice",
            "consent_granted": True,
            "consent_evidence": "verbal consent in kickoff meeting",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["display_name"] == "Alice"
    assert created.json()["voiceprint_count"] == 0
    assert created.json()["consent_granted_at"] is not None

    # Everyone can read the directory (for the transcript typeahead).
    listed = await client.get(f"{API}/speakers", headers=user_token_headers)
    assert listed.status_code == 200
    assert any(s["display_name"] == "Alice" for s in listed.json())


async def test_enroll_rejects_deactivated_profile_before_storing(
    client: AsyncClient,
    normal_user: User,
    user_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
    db_session,
) -> None:
    """Data minimization (issue #14 review): a deactivated profile's raw sample
    must be rejected at the API BEFORE it is read/stored — not accepted (202)
    and deleted later by the worker."""
    from datetime import UTC, datetime

    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile

    # Seed a REVOKED profile for the caller (with identification consent, to
    # prove it is the status — not consent — that blocks the upload).
    profile = SpeakerProfile(
        display_name="Revoked",
        user_id=normal_user.id,
        status="revoked",
        consent_granted_at=datetime.now(UTC),
    )
    db_session.add(profile)
    await db_session.flush()
    db_session.add(
        SpeakerConsentRecord(
            profile_id=profile.id,
            purpose="speaker_identification",
            status="granted",
            policy_version="v1",
            actor_id=normal_user.id,
            actor_email=normal_user.email,
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"{API}/speakers/me/voiceprints",
        headers=user_token_headers,
        files={"file": ("v.webm", b"voice", "audio/webm")},
        data={"consent_granted": "true"},
    )
    assert resp.status_code == 409
    # The upload was refused before any blob was written.
    assert speakers_enabled.blobs == {}


async def test_self_enroll_requires_consent_then_succeeds(
    client: AsyncClient,
    normal_user: User,
    user_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
) -> None:
    # No consent ⇒ rejected.
    no_consent = await client.post(
        f"{API}/speakers/me/voiceprints",
        headers=user_token_headers,
        files={"file": ("v.webm", b"voice", "audio/webm")},
        data={"consent_granted": "false"},
    )
    assert no_consent.status_code == 400

    ok = await client.post(
        f"{API}/speakers/me/voiceprints",
        headers=user_token_headers,
        files={"file": ("v.webm", b"voice", "audio/webm")},
        data={"consent_granted": "true"},
    )
    assert ok.status_code == 202, ok.text
    # A personal profile now exists.
    mine = await client.get(f"{API}/speakers/me", headers=user_token_headers)
    assert mine.status_code == 200
    assert mine.json()["user_id"] == str(normal_user.id)
    # The enrollment sample was stored and the task enqueued (mocked).
    assert len(speakers_enabled.blobs) == 1


async def test_right_to_erasure(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
) -> None:
    await client.post(
        f"{API}/speakers/me/voiceprints",
        headers=user_token_headers,
        files={"file": ("v.webm", b"voice", "audio/webm")},
        data={"consent_granted": "true"},
    )
    erased = await client.delete(
        f"{API}/speakers/me/voiceprints", headers=user_token_headers
    )
    assert erased.status_code == 204


async def _seed_enrolling_profile(
    db_session, normal_user, *, status: str = "active", consent: bool = True
):
    """A profile mid-enrollment: the blob written, the intent row present."""
    from datetime import UTC, datetime

    from app.models.audio_object import AudioObject
    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile

    profile = SpeakerProfile(
        display_name="Racer", user_id=normal_user.id, status=status
    )
    db_session.add(profile)
    await db_session.flush()
    if consent:
        db_session.add(
            SpeakerConsentRecord(
                profile_id=profile.id,
                purpose="speaker_identification",
                status="granted",
                policy_version="v1",
                actor_id=normal_user.id,
                actor_email=normal_user.email,
                created_at=datetime.now(UTC),
            )
        )
    key = f"speakers/{profile.id}/sample.webm"
    obj = AudioObject(
        bucket="b", key=key, kind="enrollment_sample", status="active", ref_id=None
    )
    db_session.add(obj)
    await db_session.commit()
    return profile, key, obj


async def test_finalize_enrollment_enqueues_on_the_happy_path(
    normal_user: User, speakers_enabled: _FakeAudioStore, db_session
) -> None:
    from sqlalchemy import select

    from app.api.v1.routes.speakers import _finalize_enrollment_upload
    from app.models.task_outbox import TaskOutbox

    profile, key, _ = await _seed_enrolling_profile(db_session, normal_user)
    speakers_enabled.blobs[key] = b"voice"

    ok = await _finalize_enrollment_upload(
        db_session,
        profile.id,
        key,
        filename="s.webm",
        user_email="u@e.com",
    )
    assert ok is True
    assert speakers_enabled.blobs.get(key) == b"voice"  # kept
    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(TaskOutbox.dedupe_key == f"enroll:{key}")
            )
        )
        .scalars()
        .all()
    )
    assert len(outbox) == 1


async def test_finalize_rearms_when_identification_consent_withdrawn(
    normal_user: User, speakers_enabled: _FakeAudioStore, db_session
) -> None:
    """Consent withdrawn/expired during the upload also cancels the enrollment
    (issue #14 review): re-arm the sample, enqueue nothing."""
    from sqlalchemy import select

    from app.api.v1.routes.speakers import _finalize_enrollment_upload
    from app.models.audio_object import AudioObject
    from app.models.task_outbox import TaskOutbox

    # Active profile + active sample, but identification consent NOT granted.
    profile, key, _ = await _seed_enrolling_profile(
        db_session, normal_user, consent=False
    )
    speakers_enabled.blobs[key] = b"voice"

    ok = await _finalize_enrollment_upload(
        db_session, profile.id, key, filename="s.webm", user_email="u@e.com"
    )
    assert ok is False
    reclaim = (
        await db_session.execute(select(AudioObject).where(AudioObject.key == key))
    ).scalar_one()
    assert reclaim.status == "delete_requested"
    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(TaskOutbox.dedupe_key == f"enroll:{key}")
            )
        )
        .scalars()
        .all()
    )
    assert outbox == []


async def test_enroll_route_returns_409_when_enrollment_is_cancelled(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public route surfaces a cancelled enrollment as 409, not a false 202
    (issue #14 review)."""
    import app.api.v1.routes.speakers as speakers_mod

    async def _cancelled(*a, **k):
        return False

    monkeypatch.setattr(speakers_mod, "_enqueue_enrollment", _cancelled)

    resp = await client.post(
        f"{API}/speakers/me/voiceprints",
        headers=user_token_headers,
        files={"file": ("v.webm", b"voice", "audio/webm")},
        data={"consent_granted": "true"},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize(
    ("kind", "obj_status", "profile_status"),
    [
        ("terminal_deleted", "deleted", "active"),
        ("profile_erased", "active", None),
        ("profile_revoked", "active", "revoked"),
    ],
)
async def test_finalize_enrollment_rearms_deletion_when_a_race_won(
    normal_user: User,
    speakers_enabled: _FakeAudioStore,
    db_session,
    kind: str,
    obj_status: str,
    profile_status,
) -> None:
    """The put-vs-delete race and its cousins (issue #14 review): the recheck
    finds the intent terminal-deleted, the profile erased, or the profile
    deactivated. The blob must be DURABLY re-armed for deletion (not a
    best-effort delete) and NO outbox enqueued."""
    from sqlalchemy import select

    from app.api.v1.routes.speakers import _finalize_enrollment_upload
    from app.models.audio_object import AudioObject
    from app.models.task_outbox import TaskOutbox

    if profile_status is None:
        # Whole-profile erasure won: no profile row, but the intent + blob exist.
        import uuid as _uuid

        pid = _uuid.uuid4()
        key = f"speakers/{pid}/sample.webm"
        db_session.add(
            AudioObject(
                bucket="b", key=key, kind="enrollment_sample", status=obj_status
            )
        )
        await db_session.commit()
    else:
        profile, key, obj = await _seed_enrolling_profile(
            db_session, normal_user, status=profile_status
        )
        pid = profile.id
        obj.status = obj_status
        await db_session.commit()
    speakers_enabled.blobs[key] = b"voice"

    ok = await _finalize_enrollment_upload(
        db_session,
        pid,
        key,
        filename="s.webm",
        user_email="u@e.com",
    )
    assert ok is False
    # Durably re-armed: a delete_requested row survives so the sweep removes it.
    reclaim = (
        await db_session.execute(select(AudioObject).where(AudioObject.key == key))
    ).scalar_one()
    assert reclaim.status == "delete_requested"
    # No enrollment was enqueued.
    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(TaskOutbox.dedupe_key == f"enroll:{key}")
            )
        )
        .scalars()
        .all()
    )
    assert outbox == []


async def test_finalize_leaves_store_delete_to_the_durable_sweep(
    normal_user: User, speakers_enabled: _FakeAudioStore, db_session
) -> None:
    """The cancel path does NOT delete the blob inline (issue #14 review): it
    re-arms the durable pipeline and the deletion sweep performs the store
    delete, so purged-version evidence is recorded in exactly one place. Here
    the store's inline delete would even RAISE — the re-arm must be unaffected."""
    from sqlalchemy import select

    from app.api.v1.routes.speakers import _finalize_enrollment_upload
    from app.models.audio_object import AudioObject

    profile, key, obj = await _seed_enrolling_profile(db_session, normal_user)
    obj.status = "deleted"
    await db_session.commit()

    def _boom(_key):
        raise RuntimeError("inline store delete must not be called")

    speakers_enabled.delete_sync = _boom  # type: ignore[method-assign]
    speakers_enabled.blobs[key] = b"voice"

    ok = await _finalize_enrollment_upload(
        db_session,
        profile.id,
        key,
        filename="s.webm",
        user_email="u@e.com",
    )
    assert ok is False
    reclaim = (
        await db_session.execute(select(AudioObject).where(AudioObject.key == key))
    ).scalar_one()
    assert reclaim.status == "delete_requested"  # armed for the durable sweep
    # No inline delete happened (it would have raised) — the sweep owns it.
    assert speakers_enabled.blobs.get(key) == b"voice"


async def test_erasure_tombstones_a_pending_enrollment_sample(
    client: AsyncClient,
    normal_user: User,
    user_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
    db_session,
) -> None:
    """Right to erasure vs a race (issue #14 review): a sample enrolled but not
    yet processed into a voiceprint (active AudioObject, ref_id NULL) must be
    tombstoned by erasure — otherwise the worker creates a voiceprint AFTER the
    erasure completed, and the profile's list-based erasure never saw it."""
    from sqlalchemy import select

    from app.models.audio_object import AudioObject
    from app.models.speaker_profile import SpeakerProfile
    from app.models.task_outbox import TaskOutbox

    # A personal profile with a PENDING enrollment sample (no voiceprint yet)
    # and its live enrollment outbox row.
    profile = SpeakerProfile(
        display_name="Racer", user_id=normal_user.id, status="active"
    )
    db_session.add(profile)
    await db_session.flush()
    pending_key = f"speakers/{profile.id}/pending.webm"
    db_session.add(
        AudioObject(
            bucket="b",
            key=pending_key,
            kind="enrollment_sample",
            status="active",
            ref_id=None,
        )
    )
    db_session.add(
        TaskOutbox(
            task_name="tasks.enroll_voiceprint",
            queue="transcription",
            payload={"profile_id": str(profile.id), "sample_key": pending_key},
            dedupe_key=f"enroll:{pending_key}",
            status="sent",
        )
    )
    await db_session.commit()

    erased = await client.delete(
        f"{API}/speakers/me/voiceprints", headers=user_token_headers
    )
    assert erased.status_code == 204

    obj = (
        await db_session.execute(
            select(AudioObject).where(AudioObject.key == pending_key)
        )
    ).scalar_one()
    await db_session.refresh(obj)
    assert obj.status == "delete_requested"
    assert obj.delete_reason == "erasure_pending_enrollment"
    # The cancelled enrollment's outbox row was retired in the same transaction.
    remaining = (
        (
            await db_session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.dedupe_key == f"enroll:{pending_key}"
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


async def test_admin_manages_profile_lifecycle(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
) -> None:
    created = await client.post(
        f"{API}/speakers",
        headers=admin_token_headers,
        json={
            "display_name": "Cleo",
            "consent_granted": True,
            "consent_evidence": "signed consent form",
        },
    )
    assert created.status_code == 201
    pid = created.json()["id"]

    # Rename + deactivate.
    patched = await client.patch(
        f"{API}/speakers/{pid}",
        headers=admin_token_headers,
        json={"display_name": "Cleopatra", "status": "revoked"},
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Cleopatra"
    assert patched.json()["status"] == "revoked"

    deleted = await client.delete(f"{API}/speakers/{pid}", headers=admin_token_headers)
    assert deleted.status_code == 204


async def test_admin_enroll_requires_profile_consent(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
) -> None:
    created = await client.post(
        f"{API}/speakers",
        headers=admin_token_headers,
        json={"display_name": "NoConsent", "consent_granted": False},
    )
    pid = created.json()["id"]
    denied = await client.post(
        f"{API}/speakers/{pid}/voiceprints",
        headers=admin_token_headers,
        files={"file": ("v.webm", b"voice", "audio/webm")},
    )
    assert denied.status_code == 400
    assert "consent" in denied.json()["detail"].lower()


async def test_speaker_endpoints_require_privilege_for_management(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    admin_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
) -> None:
    created = await client.post(
        f"{API}/speakers",
        headers=admin_token_headers,
        json={
            "display_name": "Managed",
            "consent_granted": True,
            "consent_evidence": "signed consent form",
        },
    )
    pid = created.json()["id"]
    for method, url, kwargs in [
        ("patch", f"{API}/speakers/{pid}", {"json": {"display_name": "x"}}),
        ("delete", f"{API}/speakers/{pid}", {}),
    ]:
        resp = await getattr(client, method)(url, headers=user_token_headers, **kwargs)
        assert resp.status_code == 403, f"{method} {url} -> {resp.status_code}"


# ------------------------------------------------- durable sample-object lifecycle


# ------------------------------------------------------- consent ledger (#6)


async def test_power_user_cannot_assert_consent_for_another_person(
    client: AsyncClient,
    speakers_enabled: _FakeAudioStore,
    db_session,
) -> None:
    from app.models.user import Role
    from app.models.user import User as UserModel

    power = UserModel(
        email="power@example.com",
        hashed_password="",
        role=Role.POWER_USER,
        is_active=True,
    )
    db_session.add(power)
    await db_session.flush()
    power.oidc_subject = str(power.id)
    power.oidc_issuer = settings.KEYCLOAK_ISSUER
    await db_session.commit()
    from tests.identity import create_access_token

    headers = {"Authorization": f"Bearer {create_access_token(power)}"}
    denied = await client.post(
        f"{API}/speakers",
        headers=headers,
        json={
            "display_name": "Someone",
            "consent_granted": True,
            "consent_evidence": "note",
        },
    )
    assert denied.status_code == 403


async def test_self_consent_grant_and_withdraw_ledger(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
) -> None:
    granted = await client.post(
        f"{API}/speakers/me/consents",
        headers=user_token_headers,
        json={"purpose": "speaker_identification"},
    )
    assert granted.status_code == 201, granted.text
    assert granted.json()["status"] == "granted"
    assert granted.json()["on_behalf"] is False
    assert granted.json()["policy_version"]

    withdrawn = await client.post(
        f"{API}/speakers/me/consents/withdraw",
        headers=user_token_headers,
        json={"purpose": "speaker_identification", "reason": "changed my mind"},
    )
    assert withdrawn.status_code == 201
    assert withdrawn.json()["status"] == "withdrawn"

    # The ledger keeps BOTH immutable rows, newest first.
    ledger = await client.get(f"{API}/speakers/me/consents", headers=user_token_headers)
    assert [r["status"] for r in ledger.json()] == ["withdrawn", "granted"]

    # And the withdrawn profile is no longer a matching candidate.
    me = await client.get(f"{API}/speakers/me", headers=user_token_headers)
    assert me.json()["consent_granted_at"] is None


async def test_compliance_export_request_poll_download(
    client: AsyncClient,
    db_session,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    speakers_enabled: _FakeAudioStore,
) -> None:
    """DSAR flow (#6, E5): admin-only request → durable enqueue → poll →
    audited download of the completed bundle."""
    from sqlalchemy import select

    from app.models.compliance_export import ComplianceExportJob
    from app.models.meeting_governance import MeetingAuditEvent
    from app.models.task_outbox import TaskOutbox

    # Non-admin is refused outright.
    resp = await client.post(
        f"{API}/speakers/compliance-exports",
        json={"subject_email": "subject@example.com"},
        headers=user_token_headers,
    )
    assert resp.status_code == 403

    resp = await client.post(
        f"{API}/speakers/compliance-exports",
        json={"subject_email": "Subject@Example.com"},
        headers=admin_token_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["subject_email"] == "subject@example.com"  # normalized

    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.task_name == "tasks.build_compliance_export"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(outbox) == 1

    # Download refuses until the worker completed the bundle.
    resp = await client.get(
        f"{API}/speakers/compliance-exports/{body['id']}/download",
        headers=admin_token_headers,
    )
    assert resp.status_code == 404

    job = await db_session.get(ComplianceExportJob, uuid.UUID(body["id"]))
    job.status = "completed"
    job.audio_key = "compliance/test.zip"
    job.size_bytes = 4
    speakers_enabled.blobs["compliance/test.zip"] = b"PK\x03\x04"
    await db_session.commit()

    resp = await client.get(
        f"{API}/speakers/compliance-exports",
        headers=admin_token_headers,
    )
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "completed"

    resp = await client.get(
        f"{API}/speakers/compliance-exports/{body['id']}/download",
        headers=admin_token_headers,
    )
    assert resp.status_code == 200
    assert resp.content == b"PK\x03\x04"
    assert "attachment" in resp.headers["Content-Disposition"]

    audited = (
        (
            await db_session.execute(
                select(MeetingAuditEvent).where(
                    MeetingAuditEvent.action == "compliance.export_downloaded"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audited) == 1
