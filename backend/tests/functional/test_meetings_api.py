"""Functional tests for the meeting-recording API (STT/diarization mocked out)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

import anyio
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.audio_store as audio_store_mod
import app.services.dependency_health as dependency_health
import app.services.task_outbox as task_outbox_service
from app.core.config import settings
from app.models.meeting_recording import MeetingRecording
from app.models.meeting_transcript_revision import MeetingTranscriptRevision
from app.models.meeting_transcript_segment import MeetingTranscriptSegment
from app.models.user import User

pytestmark = pytest.mark.functional

API = "/api/v1"


class _FakeAudioStore:
    """In-memory AudioStore stand-in (incl. multipart, issue #6)."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.multiparts: dict[str, dict[int, bytes]] = {}
        self.multipart_keys: dict[str, str] = {}
        self.aborted: list[str] = []

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
        data = self.blobs[key]
        yield data

    def iter_multipart_uploads(self, *, prefix: str = ""):
        return iter(
            {
                "key": self.multipart_keys.get(uid, ""),
                "upload_id": uid,
                "initiated": None,  # unknown age ⇒ the sweep must NOT abort
            }
            for uid in self.multiparts
            if self.multipart_keys.get(uid, "").startswith(prefix)
        )

    def get_range(self, key: str, start: int, end: int):
        data = self.blobs[key][start : end + 1]
        yield data

    def put_file(self, key: str, fileobj, content_type: str) -> str:
        self.blobs[key] = fileobj.read()
        return key

    def head(self, key: str) -> dict:
        data = self.blobs[key]
        return {
            "size": len(data),
            "etag": f"etag-{len(data)}",
            "content_type": "audio/webm",
        }

    def create_multipart(self, key: str, content_type: str) -> str:
        upload_id = f"mp-{len(self.multiparts) + 1}"
        self.multiparts[upload_id] = {}
        self.multipart_keys[upload_id] = key
        return upload_id

    def upload_part(
        self, key: str, upload_id: str, part_number: int, data: bytes
    ) -> str:
        self.multiparts[upload_id][part_number] = data
        return f"etag-{part_number}"

    def complete_multipart(self, key: str, upload_id: str, parts) -> None:
        stored = self.multiparts.pop(upload_id)
        self.blobs[key] = b"".join(stored[n] for n, _ in sorted(parts))

    def list_multipart_parts(self, key: str, upload_id: str) -> list[dict]:
        """Parts still held by an upload — [] once it is gone (issue #37 r17)."""
        return [{"PartNumber": n} for n in sorted(self.multiparts.get(upload_id, {}))]

    def abort_multipart(self, key: str, upload_id: str) -> bool:
        """Contract: True only when a LIVE upload was discarded (issue #37 r15)."""
        self.aborted.append(upload_id)
        return self.multiparts.pop(upload_id, None) is not None


@pytest.fixture
def meetings_enabled(monkeypatch: pytest.MonkeyPatch) -> _FakeAudioStore:
    monkeypatch.setattr(settings, "MEETINGS_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIO_S3_ENDPOINT_URL", "http://s4:7480")
    # Consent enforcement is exercised by dedicated tests; most flows opt out.
    monkeypatch.setattr(settings, "MEETING_REQUIRE_CONSENT", False)
    store = _FakeAudioStore()
    monkeypatch.setattr(audio_store_mod, "_audio_store", store)
    # Swallow broker sends: the durable outbox row is written either way; the
    # fast path "succeeds" so rows end up `sent` unless a test overrides this.
    monkeypatch.setattr(task_outbox_service, "deliver_outbox_row", lambda row: None)
    # A transcription consumer "exists" (issue #6 readiness gate) — and the
    # module-global probe cache is reset so no state leaks between tests.
    monkeypatch.setattr(
        dependency_health,
        "_snapshot_consumed_queue_names",
        lambda: frozenset({"transcription"}),
    )
    dependency_health._reset_queue_snapshot_cache()
    yield store


async def _make_workspace(
    client: AsyncClient, headers: dict[str, str], name: str = "ws"
) -> str:
    resp = await client.post(f"{API}/workspaces", headers=headers, json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_disabled_returns_503(
    client: AsyncClient, user_token_headers: dict[str, str]
) -> None:
    # Feature off by default (fixture not used) ⇒ endpoints are unavailable.
    wid = await _make_workspace(client, user_token_headers)
    resp = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert resp.status_code == 503


async def test_record_and_poll(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        data={"title": "Standup", "participant_consent": "true"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["title"] == "Standup"
    assert body["participant_consent"] is True
    # The audio was stored.
    assert len(meetings_enabled.blobs) == 1

    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    detail = await client.get(
        f"{API}/workspaces/{wid}/recordings/{body['id']}", headers=user_token_headers
    )
    assert detail.status_code == 200
    assert detail.json()["segments"] == []


async def test_empty_upload_rejected(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"", "audio/webm")},
        data={"title": "Empty"},
    )
    assert resp.status_code == 400


async def test_list_recordings_keyset_cursor_is_stable_under_inserts(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Keyset paging (review round 4): a recording created between page
    requests must not duplicate the boundary row or hide an older one, the
    way a shifted offset does."""
    from datetime import UTC, datetime, timedelta

    wid = await _make_workspace(client, user_token_headers)
    base = datetime.now(UTC)

    def _rec(title: str, age_minutes: int) -> MeetingRecording:
        return MeetingRecording(
            workspace_id=uuid.UUID(wid),
            title=title,
            status="completed",
            audio_key=f"k-{title}",
            audio_content_type="audio/webm",
            created_by_id=normal_user.id,
            created_by_email=normal_user.email,
            created_at=base - timedelta(minutes=age_minutes),
        )

    db_session.add_all([_rec("oldest", 30), _rec("middle", 20), _rec("newest", 10)])
    await db_session.commit()

    page1 = await client.get(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        params={"limit": 2},
    )
    assert [r["title"] for r in page1.json()] == ["newest", "middle"]
    boundary = page1.json()[-1]

    # A recording lands between the two page requests — with offsets this
    # would push "middle" into page 2 as a duplicate.
    db_session.add(_rec("inserted", 5))
    await db_session.commit()

    page2 = await client.get(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        params={
            "limit": 2,
            "before_created_at": boundary["created_at"],
            "before_id": boundary["id"],
        },
    )
    assert [r["title"] for r in page2.json()] == ["oldest"]

    # A half cursor is a client bug — reject it loudly instead of silently
    # serving page one again (review round 5).
    half = await client.get(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        params={"before_id": boundary["id"]},
    )
    assert half.status_code == 400


async def test_non_member_cannot_record(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    admin_token_headers: dict[str, str],
    normal_user: User,
    meetings_enabled: _FakeAudioStore,
) -> None:
    # A private workspace owned by the admin cannot be read by an unrelated,
    # non-member normal user (cross-workspace isolation on GET).
    other = await _make_workspace(client, admin_token_headers, name="admins")
    resp = await client.get(
        f"{API}/workspaces/{other}/recordings", headers=user_token_headers
    )
    # normal user is not a member of the admin's private workspace.
    assert resp.status_code == 403


async def _seed_completed_recording(
    session: AsyncSession, workspace_id: uuid.UUID, owner: User
) -> MeetingRecording:
    rec = MeetingRecording(
        workspace_id=workspace_id,
        title="Seeded",
        status="completed",
        audio_key="k1",
        audio_content_type="audio/webm",
        created_by_id=owner.id,
        created_by_email=owner.email,
    )
    session.add(rec)
    await session.flush()
    # Give review and export tests a completed recording with an approved
    # revision and explicit segment ownership.
    revision = MeetingTranscriptRevision(
        recording_id=rec.id, revision_number=1, state="approved"
    )
    session.add(revision)
    await session.flush()
    session.add_all(
        [
            MeetingTranscriptSegment(
                recording_id=rec.id,
                revision_id=revision.id,
                idx=0,
                start_ms=0,
                end_ms=1000,
                speaker_label="SPEAKER_00",
                text="Hello everyone.",
            ),
            MeetingTranscriptSegment(
                recording_id=rec.id,
                revision_id=revision.id,
                idx=1,
                start_ms=1000,
                end_ms=2000,
                speaker_label="SPEAKER_01",
                text="Hi there.",
            ),
        ]
    )
    rec.latest_revision_id = revision.id
    rec.approved_revision_id = revision.id
    await session.commit()
    await session.refresh(rec)
    return rec


async def test_unsupported_extension_rejected(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("notes.pdf", b"%PDF", "application/pdf")},
        data={"title": "Nope"},
    )
    assert resp.status_code == 400
    assert "Unsupported audio format" in resp.json()["detail"]


async def test_audio_playback_streams_and_404s_after_purge(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"RIFF-fake-wav-bytes"
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    audio = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio",
        headers=user_token_headers,
    )
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/")
    assert audio.content == b"RIFF-fake-wav-bytes"

    # Retention purged the blob -> playback is a clear 404, not a 500.
    rec.audio_deleted = True
    rec.audio_key = None
    await db_session.commit()
    gone = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio",
        headers=user_token_headers,
    )
    assert gone.status_code == 404


async def test_retry_reruns_terminal_recording_safely(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio"
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    # Retrying a COMPLETED recording is safe since transcript revisions: the
    # rerun persists a NEW candidate revision and never touches this one.
    retried = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/retry",
        headers=user_token_headers,
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "pending"

    # Now "in flight" -> a second retry conflicts.
    conflict = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/retry",
        headers=user_token_headers,
    )
    assert conflict.status_code == 409

    # And a recording whose audio was purged can't be retried either.
    await db_session.refresh(rec)
    rec.status = "failed"
    rec.audio_key = None
    rec.audio_deleted = True
    await db_session.commit()
    no_audio = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/retry",
        headers=user_token_headers,
    )
    assert no_audio.status_code == 409


async def _seed_failed_recording(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    owner: User,
    *,
    error_code: str,
) -> MeetingRecording:
    rec = MeetingRecording(
        workspace_id=workspace_id,
        title="Failed",
        status="failed",
        audio_key="k1",
        audio_content_type="audio/webm",
        error="a safe message",
        error_code=error_code,
        created_by_id=owner.id,
        created_by_email=owner.email,
    )
    session.add(rec)
    await session.commit()
    await session.refresh(rec)
    return rec


async def test_retry_blocked_for_non_retryable_media_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """A non-retryable media fault (issue #37) fails identically on the same
    object, so the retry endpoint refuses it with guidance and leaves the row
    untouched — no dead-end pending state."""
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio"
    rec = await _seed_failed_recording(
        db_session,
        uuid.UUID(wid),
        normal_user,
        error_code="recording_invalid_container",
    )
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/retry",
        headers=user_token_headers,
    )
    assert resp.status_code == 409
    assert "won't help" in resp.json()["detail"]
    # The row is unchanged — still failed with its stable code.
    await db_session.refresh(rec)
    assert rec.status == "failed"
    assert rec.error_code == "recording_invalid_container"


async def test_retry_allowed_for_retryable_failure_clears_code(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """A retryable infra failure can be retried, and the reset clears BOTH the
    message and the stable error_code (not just error) so a fresh attempt starts
    clean (issue #37 review)."""
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio"
    rec = await _seed_failed_recording(
        db_session,
        uuid.UUID(wid),
        normal_user,
        error_code="transcription_service_unavailable",
    )
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/retry",
        headers=user_token_headers,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["error"] is None
    assert body["error_code"] is None


async def test_viewer_cannot_record_but_can_read(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    # Admin owns the workspace; normal user is added as a VIEWER.
    wid = await _make_workspace(client, admin_token_headers, name="viewer-ws")
    added = await client.post(
        f"{API}/workspaces/{wid}/members",
        headers=admin_token_headers,
        json={"user_id": str(normal_user.id), "role": "viewer"},
    )
    assert added.status_code in (200, 201), added.text

    listing = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert listing.status_code == 200  # read is fine

    denied = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"x", "audio/webm")},
        data={"title": "nope"},
    )
    assert denied.status_code == 403  # content-level action


async def test_config_exposes_meetings_flag(
    client: AsyncClient, meetings_enabled: _FakeAudioStore
) -> None:
    resp = await client.get(f"{API}/config")
    assert resp.status_code == 200
    assert resp.json()["meetings_enabled"] is True


async def test_assign_speaker_to_all_segments_with_label(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    admin_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Bulk 'I recognize Speaker 1 — that's Bob': every SPEAKER_00 segment gets
    the person; other labels stay untouched; clearing works; unknown profile 404s.
    """
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    # A third segment sharing SPEAKER_00 so the bulk update covers >1 row.
    db_session.add(
        MeetingTranscriptSegment(
            recording_id=rec.id,
            revision_id=rec.latest_revision_id,
            idx=2,
            start_ms=2000,
            end_ms=3000,
            speaker_label="SPEAKER_00",
            text="Back again.",
        )
    )
    await db_session.commit()

    # A directory entry to assign (created by the admin; no voiceprint needed).
    speaker = await client.post(
        f"{API}/speakers",
        headers=admin_token_headers,
        json={"display_name": "Bob", "consent_granted": False},
    )
    bob_id = speaker.json()["id"]

    assigned = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/assign-speaker",
        headers=user_token_headers,
        json={"speaker_label": "SPEAKER_00", "speaker_profile_id": bob_id},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json() == {"updated_segments": 2}

    detail = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}", headers=user_token_headers
    )
    segs = detail.json()["segments"]
    by_label = {}
    for s in segs:
        by_label.setdefault(s["speaker_label"], []).append(s)
    assert all(s["speaker_name"] == "Bob" for s in by_label["SPEAKER_00"])
    assert all(s["is_edited"] for s in by_label["SPEAKER_00"])
    assert all(s["speaker_confidence"] is None for s in by_label["SPEAKER_00"])
    assert by_label["SPEAKER_01"][0]["speaker_name"] is None  # untouched

    # Clear the label back to anonymous.
    cleared = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/assign-speaker",
        headers=user_token_headers,
        json={"speaker_label": "SPEAKER_00", "clear_speaker": True},
    )
    assert cleared.json() == {"updated_segments": 2}

    # Unknown profile -> 404 (nothing half-applied).
    missing = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/assign-speaker",
        headers=user_token_headers,
        json={
            "speaker_label": "SPEAKER_00",
            "speaker_profile_id": "00000000-0000-0000-0000-000000000009",
        },
    )
    assert missing.status_code == 404


# ------------------------------------------------- durable audio-object lifecycle


async def test_create_recording_registers_audio_object(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    from sqlalchemy import select

    from app.models.audio_object import AudioObject

    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        data={"title": "Tracked", "participant_consent": "true"},
    )
    assert resp.status_code == 202, resp.text
    rec = resp.json()

    obj = (
        (await db_session.execute(select(AudioObject))).scalars().one()
    )  # exactly one tracked object
    assert obj.status == "active"
    assert obj.kind == "meeting_recording"
    assert obj.ref_id == uuid.UUID(rec["id"])
    assert obj.workspace_id == uuid.UUID(wid)
    assert obj.key in meetings_enabled.blobs
    assert obj.size_bytes == len(b"fake-audio-bytes")


async def test_delete_recording_leaves_durable_deletion_tombstone(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    """Deleting a recording must not fire-and-forget the blob delete: the row is
    removed, but the object gets a durable ``delete_requested`` tombstone that the
    deletion sweep drives to confirmation (issue #6, wave A1)."""
    from sqlalchemy import select

    from app.models.audio_object import AudioObject

    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        data={"title": "Doomed"},
    )
    assert resp.status_code == 202
    rid = resp.json()["id"]

    deleted = await client.delete(
        f"{API}/workspaces/{wid}/recordings/{rid}", headers=user_token_headers
    )
    assert deleted.status_code == 204

    # The recording row is gone…
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert listed.json() == []

    # …but the blob is still in the store, tracked by a tombstone that preserves
    # the key and records who asked and why.
    assert len(meetings_enabled.blobs) == 1
    obj = (await db_session.execute(select(AudioObject))).scalars().one()
    assert obj.status == "delete_requested"
    assert obj.delete_reason == "user_request"
    assert obj.delete_requested_by_email is not None
    assert obj.key in meetings_enabled.blobs


async def test_workspace_delete_tombstones_meeting_audio(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    """Deleting a workspace CASCADEs the recording rows away — without a durable
    tombstone the blobs would orphan in the object store forever."""
    from sqlalchemy import select

    from app.models.audio_object import AudioObject

    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        data={"title": "Orphan-me-not"},
    )
    assert resp.status_code == 202

    deleted = await client.delete(f"{API}/workspaces/{wid}", headers=user_token_headers)
    assert deleted.status_code in (200, 204), deleted.text

    obj = (await db_session.execute(select(AudioObject))).scalars().one()
    assert obj.status == "delete_requested"
    assert obj.delete_reason == "workspace_delete"
    assert obj.key in meetings_enabled.blobs  # blob still there, tracked


async def test_create_rejected_when_no_transcription_worker(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness gate (issue #6): work that no worker could ever pick up is
    refused up front with actionable guidance, not accepted into a dead queue."""
    monkeypatch.setattr(
        dependency_health, "_snapshot_consumed_queue_names", lambda: frozenset()
    )
    dependency_health._reset_queue_snapshot_cache()
    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"bytes", "audio/webm")},
        data={"title": "Doomed"},
    )
    assert resp.status_code == 503
    assert "transcription" in resp.json()["detail"]


async def test_client_event_is_recorded_and_bounded(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Browser-side recorder diagnostics (issue #37): a stable category plus
    coarse technical context is accepted; anything else is refused, so no
    user-generated text can reach the logs or the metric label."""
    from app.core.metrics import MEETING_CLIENT_EVENTS

    wid = await _make_workspace(client, user_token_headers)
    counter = MEETING_CLIENT_EVENTS.labels(event="preflight_bad_header")
    before = counter._value.get()

    resp = await client.post(
        f"{API}/workspaces/{wid}/meetings/client-events",
        headers=user_token_headers,
        json={
            "event": "preflight_bad_header",
            "browser_family": "Chrome",
            "browser_major": 126,
            "container": "webm",
            "codec": "opus",
            "chunk_count": 12,
        },
    )
    assert resp.status_code == 202, resp.text
    assert counter._value.get() - before == 1

    # An unknown category is rejected — the metric label set stays closed.
    bad = await client.post(
        f"{API}/workspaces/{wid}/meetings/client-events",
        headers=user_token_headers,
        json={"event": "something_invented"},
    )
    assert bad.status_code == 422


async def test_client_event_rejects_injected_log_content(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """The endpoint is browser-callable, so every field must be a closed enum,
    a bounded int, or a strict MIME. A free-text field here would be a
    log-INJECTION and data-exfiltration channel (issue #37 review round 5)."""
    wid = await _make_workspace(client, user_token_headers)

    async def post(payload: dict) -> int:
        resp = await client.post(
            f"{API}/workspaces/{wid}/meetings/client-events",
            headers=user_token_headers,
            json={"event": "preflight_empty", **payload},
        )
        return resp.status_code

    # Newline injection: would otherwise forge an extra structured log line.
    assert await post({"browser_family": "token=secret\nforged=true"}) == 422
    assert await post({"container": "audio/webm\nforged=true"}) == 422
    # Control characters and whitespace.
    assert await post({"container": "audio/webm\r\n"}) == 422
    assert await post({"container": "audio/ webm"}) == 422
    assert await post({"container": "audio/webm\x00"}) == 422
    # Filesystem paths / URLs / secrets must not be smuggled through.
    assert await post({"container": "/tmp/customer/recording.webm"}) == 422
    assert await post({"container": "https://evil.example/?t=secret"}) == 422
    assert await post({"browser_family": "/tmp/customer"}) == 422
    # A MIME-SHAPED value is still caller-chosen text: the container is a closed
    # token set, so neither of these can reach the log (review round 6).
    assert await post({"container": "secret/topsecret"}) == 422
    assert await post({"container": "audio/webm;token=secret"}) == 422
    assert await post({"container": "audio/webm"}) == 422  # not a bare token
    assert await post({"codec": "opus;leak=1"}) == 422
    # An unknown browser family is refused (the log value stays closed).
    assert await post({"browser_family": "Chrome 126"}) == 422
    assert await post({"browser_major": 99999}) == 422
    # Unknown fields are rejected outright rather than silently ignored.
    assert await post({"note": "arbitrary text"}) == 422
    # The infra codes may NOT be attributed to a media rejection.
    assert (
        await post(
            {"event": "media_rejected", "error_code": "transcription_internal_error"}
        )
        == 422
    )

    # ...while the legitimate shape still passes.
    assert (
        await post(
            {
                "browser_family": "Firefox",
                "browser_major": 128,
                "container": "ogg",
                "codec": "opus",
                "missing_count": 2,
                "duplicate_count": 0,
                "max_gap_ms": 4200,
                "total_bytes": 1024,
            }
        )
        == 202
    )


async def test_media_rejected_cannot_be_reported_by_a_client(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Only the WORKER may report a media rejection (issue #37 r7).

    A client-submitted verdict was both forgeable (any member with RECORD could
    attribute one to someone else's recording) and replayable (every reload
    re-sending it). The category is simply not in the client's vocabulary now,
    so neither is possible — the worker emits it once, inside its terminal CAS.
    """
    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/meetings/client-events",
        headers=user_token_headers,
        json={"event": "media_rejected"},
    )
    assert resp.status_code == 422
    # ...and neither are the correlation fields it used to carry.
    resp = await client.post(
        f"{API}/workspaces/{wid}/meetings/client-events",
        headers=user_token_headers,
        json={
            "event": "preflight_empty",
            "recording_id": str(uuid.uuid4()),
            "error_code": "recording_invalid_container",
        },
    )
    assert resp.status_code == 422


async def test_upload_completion_stores_client_diagnostics(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Diagnostics ride along with the upload as validated closed tokens, so the
    worker can attach them to a rejection it emits later (issue #37 r7)."""
    from sqlalchemy import select

    wid = await _make_workspace(client, user_token_headers)
    opened = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads",
        headers=user_token_headers,
        json={
            "title": "Diag",
            "filename": "m.webm",
            "size_bytes": 5,
            "participant_consent": True,
        },
    )
    assert opened.status_code == 201, opened.text
    session_id = opened.json()["id"]
    body = b"audio"
    digest = hashlib.sha256(body).hexdigest()
    part = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{session_id}/parts/1",
        headers={**user_token_headers, "X-Part-Sha256": digest},
        content=body,
    )
    assert part.status_code in (200, 201, 204), part.text

    done = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{session_id}/complete",
        headers=user_token_headers,
        json={
            "parts": [{"part_number": 1, "sha256": digest}],
            "client_diagnostics": {
                "browser_family": "Chrome",
                "browser_major": 126,
                "container": "webm",
                "codec": "opus",
                "chunk_count": 3,
                "max_gap_ms": 1200,
                "max_media_gap_ms": 1000,
                "max_skew_ms": 200,
            },
        },
    )
    assert done.status_code == 202, done.text

    rec = (
        await db_session.execute(
            select(MeetingRecording).where(
                MeetingRecording.id == uuid.UUID(done.json()["id"])
            )
        )
    ).scalar_one()
    await db_session.refresh(rec)
    assert rec.client_diagnostics["browser_family"] == "Chrome"
    assert rec.client_diagnostics["container"] == "webm"
    assert rec.client_diagnostics["max_skew_ms"] == 200

    # Free text still cannot ride in through this door either.
    bad = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{session_id}/complete",
        headers=user_token_headers,
        json={
            "parts": [{"part_number": 1, "sha256": digest}],
            "client_diagnostics": {"container": "secret/topsecret"},
        },
    )
    assert bad.status_code == 422


async def test_repeated_completion_is_idempotent(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """A completion that the client did not see the answer to must be safe to
    repeat (issue #37 r8). An ambiguous 409 previously led the client to discard
    its ids and upload the SAME meeting a second time."""
    wid = await _make_workspace(client, user_token_headers)
    opened = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads",
        headers=user_token_headers,
        json={
            "title": "Twice",
            "filename": "m.webm",
            "size_bytes": 5,
            "participant_consent": True,
        },
    )
    session_id = opened.json()["id"]
    body = b"audio"
    digest = hashlib.sha256(body).hexdigest()
    await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{session_id}/parts/1",
        headers={**user_token_headers, "X-Part-Sha256": digest},
        content=body,
    )
    manifest = {"parts": [{"part_number": 1, "sha256": digest}]}

    first = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{session_id}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert first.status_code == 202, first.text

    # The SAME completion again returns the SAME recording, not a conflict...
    second = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{session_id}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert second.status_code == 202, second.text
    assert second.json()["id"] == first.json()["id"]

    # ...and exactly one recording exists for the workspace.
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert len(listed.json()) == 1


async def _open_and_fill_upload(
    client: AsyncClient, headers: dict[str, str], wid: str, title: str = "Race"
) -> tuple[str, dict]:
    """Open an upload session and PUT its single part; returns (sid, manifest)."""
    opened = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads",
        headers=headers,
        json={
            "title": title,
            "filename": "m.webm",
            "size_bytes": 5,
            "participant_consent": True,
        },
    )
    assert opened.status_code == 201, opened.text
    sid = opened.json()["id"]
    body = b"audio"
    digest = hashlib.sha256(body).hexdigest()
    part = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/1",
        headers={**headers, "X-Part-Sha256": digest},
        content=body,
    )
    assert part.status_code in (200, 201, 204), part.text
    return sid, {"parts": [{"part_number": 1, "sha256": digest}]}


async def test_completion_in_flight_is_explicitly_retryable(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the FIRST completion is still finishing, a second call gets an
    explicit RETRYABLE outcome (issue #37 review rounds 9-11).

    Not "no longer active" (which made the client discard its ids and record the
    meeting twice) and not a success either — answering 202 there made the
    client stop calling completion at all, so a holder that then died stranded
    the recording with nobody to reclaim it.
    """
    wid = await _make_workspace(client, user_token_headers)
    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)

    second: dict[str, object] = {}
    original = meetings_enabled.complete_multipart

    def _blocked(key: str, upload_id: str, parts) -> None:
        async def _race() -> None:
            resp = await client.post(
                f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
                headers=user_token_headers,
                json=manifest,
            )
            second["status"] = resp.status_code

        anyio.from_thread.run(_race)
        original(key, upload_id, parts)

    monkeypatch.setattr(meetings_enabled, "complete_multipart", _blocked)

    first = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert first.status_code == 202, first.text
    # 425 Too Early: come back — NOT 409 (which the client reads as dead).
    assert second["status"] == 425, second

    # And a retry now converges on the finished recording.
    again = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert again.status_code == 202, again.text
    assert again.json()["id"] == first.json()["id"]
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert len(listed.json()) == 1


async def test_cancel_during_an_in_flight_completion_is_refused(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel destroys STORAGE, so it must take the lease like every other
    writer (issue #37 review round 12).

    A cancel landing while a completion holds a FRESH lease would otherwise
    abort the multipart upload underneath it — losing a recording the user is
    about to be told succeeded. The server refuses instead, and the completion
    finishes intact.
    """
    wid = await _make_workspace(client, user_token_headers)
    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)

    cancel: dict[str, object] = {}
    original = meetings_enabled.complete_multipart

    def _blocked(key: str, upload_id: str, parts) -> None:
        async def _race() -> None:
            resp = await client.delete(
                f"{API}/workspaces/{wid}/meetings/uploads/{sid}",
                headers=user_token_headers,
            )
            cancel["status"] = resp.status_code

        anyio.from_thread.run(_race)
        original(key, upload_id, parts)

    monkeypatch.setattr(meetings_enabled, "complete_multipart", _blocked)

    first = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert first.status_code == 202, first.text
    assert cancel["status"] == 409, cancel
    # The live multipart upload was never aborted...
    assert meetings_enabled.aborted == []
    # ...and the recording the user was completing still exists.
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert [r["id"] for r in listed.json()] == [first.json()["id"]]


async def test_a_fenced_session_stops_accepting_parts(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Erasure fences the session, and the part endpoint honours it (r17).

    Aborting a multipart upload does not stop a client that is still uploading:
    each new part re-creates the condition erasure has to prove absent, so
    without a fence the store's abort/verify loop could never converge and
    erasure would raise forever. `expiring` is the state that closes the tap —
    this proves the API side of that contract, which the store loop relies on.
    """
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app.models.meeting_upload_session import MeetingUploadSession

    wid = await _make_workspace(client, user_token_headers)
    sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)

    # The state the deletion worker's fence puts a live session in. (That the
    # fence WRITES this state, and waits for admitted writes to drain, is
    # covered by the worker-level tests; here it is the API half.)
    await db_session.execute(
        update(MeetingUploadSession)
        .where(MeetingUploadSession.id == uuid.UUID(sid))
        .values(status="expiring", completing_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()

    body = b"late"
    late = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/2",
        headers={
            **user_token_headers,
            "X-Part-Sha256": hashlib.sha256(body).hexdigest(),
        },
        content=body,
    )
    assert late.status_code == 409, late.text
    # ...and completion is refused too, so no new object can appear either.
    completed = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json={"parts": [{"part_number": 1, "sha256": "0" * 64}]},
    )
    assert completed.status_code == 409, completed.text


async def test_a_part_write_releases_its_admission(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished part must not leave its admission behind (issue #37 r17b).

    The counter is what erasure waits to drain, so a leaked one would stall
    every future deletion of this key until it ages out. It is released in a
    `finally`, so a FAILED write must clear it too.
    """
    from sqlalchemy import select

    from app.crud import meeting_upload as meeting_upload_crud
    from app.models.meeting_upload_session import MeetingUploadSession

    wid = await _make_workspace(client, user_token_headers)
    sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)
    session_id = uuid.UUID(sid)

    async def _inflight() -> int:
        row = (
            await db_session.execute(
                select(MeetingUploadSession).where(
                    MeetingUploadSession.id == session_id
                )
            )
        ).scalar_one()
        await db_session.refresh(row)
        return row.inflight_writes

    assert await _inflight() == 0  # the part uploaded during setup released it

    # Observed from INSIDE the request, right where the deletion fence would
    # race it: the admission must already be visible, or the fence has nothing
    # to wait for and erasure proceeds under a live writer.
    seen: dict[str, int] = {}
    original_upsert = meeting_upload_crud.upsert_part

    async def _observe(sess, **kwargs):
        row = (
            await sess.execute(
                select(MeetingUploadSession)
                .where(MeetingUploadSession.id == session_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        seen["inflight"] = row.inflight_writes
        return await original_upsert(sess, **kwargs)

    monkeypatch.setattr(meeting_upload_crud, "upsert_part", _observe)

    body = b"second"
    ok = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/2",
        headers={
            **user_token_headers,
            "X-Part-Sha256": hashlib.sha256(body).hexdigest(),
        },
        content=body,
    )
    assert ok.status_code == 200, ok.text
    assert seen["inflight"] == 1, "the write was not admitted/accounted for"
    monkeypatch.setattr(meeting_upload_crud, "upsert_part", original_upsert)
    assert await _inflight() == 0

    # A REJECTED write (checksum mismatch) must release it as well.
    bad = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/3",
        headers={**user_token_headers, "X-Part-Sha256": "0" * 64},
        content=b"third",
    )
    assert bad.status_code == 422, bad.text
    assert await _inflight() == 0


def test_the_heartbeat_cadence_is_well_inside_the_leak_cutoff() -> None:
    """The freshness cutoff is only sound because the heartbeat outpaces it.

    A counter is treated as leaked once `inflight_since` is older than the
    lease. That judgement is safe only if a LIVE writer refreshes faster than
    the cutoff — with room for missed beats. Three beats inside one lease keeps
    two consecutive failures survivable (issue #37 review round 18).
    """
    from app.api.v1.routes.meetings import (
        COMPLETION_LEASE_SECONDS,
        WRITE_HEARTBEAT_SECONDS,
    )

    assert WRITE_HEARTBEAT_SECONDS * 3 <= COMPLETION_LEASE_SECONDS


async def test_cancel_waits_for_an_admitted_part_write_to_drain(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window the r17b drain did not cover (issue #37 review round 18).

    Cancellation claims the session — which stops NEW admissions — but a part
    write admitted a moment earlier is still streaming, and AWS permits that
    UploadPart to succeed after the abort. A "clean" 204 then deletes the
    session and recording, and the late parts belong to NOTHING: the key has no
    row left, so even the multipart reconciler skips it.

    Here the REAL part write is held open in the store while cancellation is
    attempted — not injected into the abort, which is the interleaving the
    earlier regressions missed. Cancel must refuse (retryably) until the
    writer settles, and succeed after.
    """
    import threading

    from app.api.v1.routes import meetings as meetings_routes

    wid = await _make_workspace(client, user_token_headers)
    sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)

    entered = threading.Event()
    release = threading.Event()
    original = meetings_enabled.upload_part

    def _held(key, upload_id, part_number, data):
        entered.set()
        assert release.wait(10), "test gate never released"
        return original(key, upload_id, part_number, data)

    monkeypatch.setattr(meetings_enabled, "upload_part", _held)
    body = b"held-part"
    put_task = asyncio.create_task(
        client.put(
            f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/2",
            headers={
                **user_token_headers,
                "X-Part-Sha256": hashlib.sha256(body).hexdigest(),
            },
            content=body,
        )
    )
    while not entered.is_set():
        await asyncio.sleep(0.01)

    # The write is admitted and mid-stream. Cancellation must not reach
    # storage past it — and must not block the request forever either.
    monkeypatch.setattr(meetings_routes, "CANCEL_DRAIN_SECONDS", 0.3)
    monkeypatch.setattr(meetings_routes, "_CANCEL_DRAIN_POLL_SECONDS", 0.02)
    refused = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert refused.status_code == 503, refused.text
    assert meetings_enabled.aborted == []  # storage untouched under a writer

    release.set()
    finished = await put_task
    assert finished.status_code == 200, finished.text
    monkeypatch.setattr(meetings_enabled, "upload_part", original)

    # The claim was RELEASED on refusal, so the retry succeeds cleanly.
    cancelled = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert cancelled.status_code == 204, cancelled.text
    assert len(meetings_enabled.aborted) == 1


async def test_a_slow_writer_heartbeats_and_never_looks_leaked(
    client: AsyncClient,
    db_session: AsyncSession,
    engine,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely live writer must never age into "leaked" (issue #37 r18).

    The freshness cutoff reaps counters whose process died. Without a
    heartbeat, a slow client streaming a large part would cross the cutoff
    while alive, and a later erasure retry would proceed around it. The write
    refreshes `inflight_since` for as long as it actually runs.
    """
    import threading

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.db.session as db_session_module
    from app.api.v1.routes import meetings as meetings_routes
    from app.models.meeting_upload_session import MeetingUploadSession

    # The heartbeat runs on its own session; point it at the test database.
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(meetings_routes, "WRITE_HEARTBEAT_SECONDS", 0.02)

    wid = await _make_workspace(client, user_token_headers)
    sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)
    session_id = uuid.UUID(sid)

    async def _inflight() -> tuple[int, object]:
        row = (
            await db_session.execute(
                select(MeetingUploadSession)
                .where(MeetingUploadSession.id == session_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return row.inflight_writes, row.inflight_since

    entered = threading.Event()
    release = threading.Event()
    original = meetings_enabled.upload_part

    def _held(key, upload_id, part_number, data):
        entered.set()
        assert release.wait(10), "test gate never released"
        return original(key, upload_id, part_number, data)

    monkeypatch.setattr(meetings_enabled, "upload_part", _held)
    body = b"slow-part"
    put_task = asyncio.create_task(
        client.put(
            f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/2",
            headers={
                **user_token_headers,
                "X-Part-Sha256": hashlib.sha256(body).hexdigest(),
            },
            content=body,
        )
    )
    while not entered.is_set():
        await asyncio.sleep(0.01)

    count, first_beat = await _inflight()
    assert count == 1 and first_beat is not None
    # The write only STREAMS from here — yet its timestamp keeps advancing.
    deadline = time.monotonic() + 5
    while True:
        _count, beat = await _inflight()
        if beat > first_beat:
            break
        assert time.monotonic() < deadline, "heartbeat never refreshed the write"
        await asyncio.sleep(0.02)

    release.set()
    finished = await put_task
    assert finished.status_code == 200, finished.text
    monkeypatch.setattr(meetings_enabled, "upload_part", original)
    assert (await _inflight())[0] == 0  # released once the write ended


async def test_a_stranger_cannot_cancel_someone_elses_upload(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Cancellation DESTROYS another person's recording (issue #37 r14).

    Workspace content access is not a licence to do that: an editor could
    otherwise discard a colleague's upload mid-flight — the very act the DELETE
    capability guards once the recording exists. The uploader may always cancel
    their own; anyone else needs DELETE.
    """
    wid = await _make_workspace(client, admin_token_headers, name="shared")
    added = await client.post(
        f"{API}/workspaces/{wid}/members",
        headers=admin_token_headers,
        json={"user_id": str(normal_user.id), "role": "editor"},
    )
    assert added.status_code in (200, 201), added.text
    # Editors may record and upload, but not delete.
    revoked = await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=admin_token_headers,
        json={"capability": "delete", "allow": False, "role": "editor"},
    )
    assert revoked.status_code in (200, 201), revoked.text

    # The ADMIN opens the upload; the editor tries to cancel it.
    sid, _ = await _open_and_fill_upload(client, admin_token_headers, wid)
    stranger = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert stranger.status_code == 403, stranger.text
    assert meetings_enabled.aborted == []  # nothing was destroyed

    # Granting DELETE makes it an explicit, auditable administrative action.
    granted = await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=admin_token_headers,
        json={"capability": "delete", "allow": True, "role": "editor"},
    )
    assert granted.status_code in (200, 201), granted.text
    allowed = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert allowed.status_code == 204, allowed.text

    # ...and the owner never needs the capability for their own upload.
    own_sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)
    revoked_again = await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=admin_token_headers,
        json={"capability": "delete", "allow": False, "role": "editor"},
    )
    assert revoked_again.status_code in (200, 201), revoked_again.text
    own = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{own_sid}",
        headers=user_token_headers,
    )
    assert own.status_code == 204, own.text


async def test_session_writes_are_bound_to_the_uploader(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """An upload session belongs to ONE person (issue #37 review round 15).

    The session id is just a UUID the client holds; workspace content access is
    not a licence to write to someone else's in-flight recording. Another editor
    must not be able to overwrite its parts or complete it on their behalf.
    """
    wid = await _make_workspace(client, admin_token_headers, name="notmine")
    added = await client.post(
        f"{API}/workspaces/{wid}/members",
        headers=admin_token_headers,
        json={"user_id": str(normal_user.id), "role": "editor"},
    )
    assert added.status_code in (200, 201), added.text

    # The ADMIN opens the session; the editor knows its id.
    sid, manifest = await _open_and_fill_upload(client, admin_token_headers, wid)
    body = b"hostile"
    hijack_part = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/1",
        headers={
            **user_token_headers,
            "X-Part-Sha256": hashlib.sha256(body).hexdigest(),
        },
        content=body,
    )
    assert hijack_part.status_code == 403, hijack_part.text
    hijack_complete = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert hijack_complete.status_code == 403, hijack_complete.text

    # The owner is unaffected.
    ok = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=admin_token_headers,
        json=manifest,
    )
    assert ok.status_code == 202, ok.text


async def test_session_writes_re_check_the_recording_capability(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Revoking RECORD must stop an upload ALREADY under way.

    Only session creation checked the capability, so a member whose grant was
    withdrawn mid-upload could still push parts and complete the recording —
    the revocation did nothing until their next recording (issue #37 r15).
    """
    wid = await _make_workspace(client, admin_token_headers, name="revoked")
    added = await client.post(
        f"{API}/workspaces/{wid}/members",
        headers=admin_token_headers,
        json={"user_id": str(normal_user.id), "role": "editor"},
    )
    assert added.status_code in (200, 201), added.text

    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)
    revoked = await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=admin_token_headers,
        json={"capability": "record", "allow": False, "role": "editor"},
    )
    assert revoked.status_code in (200, 201), revoked.text

    body = b"more"
    late_part = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/2",
        headers={
            **user_token_headers,
            "X-Part-Sha256": hashlib.sha256(body).hexdigest(),
        },
        content=body,
    )
    assert late_part.status_code == 403, late_part.text
    late_complete = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert late_complete.status_code == 403, late_complete.text
    assert meetings_enabled.blobs == {}  # nothing was ever stored


async def test_cancelling_a_completed_upload_tombstones_the_object(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel that finds the multipart upload already COMPLETE must not
    orphan the blob (issue #37 review round 14).

    The owner died between the store accepting the object and the transaction
    that registers it. `abort_multipart` raises NoSuchUpload there — treated as
    "nothing to abort" — and deleting the session and recording then removes the
    last thing that could ever find the object. Cancellation runs the same
    reconciliation the expiry sweep does: register it, then tombstone it through
    the confirmed-deletion lifecycle.
    """
    from sqlalchemy import select

    from app.models.audio_object import AudioObject

    wid = await _make_workspace(client, user_token_headers)
    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)

    # The completion stores the object and then dies before registering it.
    original = meetings_enabled.complete_multipart

    def _store_then_die(key, upload_id, parts):
        original(key, upload_id, parts)
        raise RuntimeError("owner died after storing")

    monkeypatch.setattr(meetings_enabled, "complete_multipart", _store_then_die)
    with pytest.raises(RuntimeError):
        await client.post(
            f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
            headers=user_token_headers,
            json=manifest,
        )
    monkeypatch.setattr(meetings_enabled, "complete_multipart", original)
    from app.api.v1.routes import meetings as meetings_routes

    monkeypatch.setattr(meetings_routes, "COMPLETION_LEASE_SECONDS", 0)

    cancelled = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert cancelled.status_code == 204, cancelled.text

    # No bogus abort of an upload that no longer exists...
    assert meetings_enabled.aborted == []
    # ...and the stored object is now attributable and queued for deletion,
    # instead of sitting in the bucket with nothing pointing at it.
    tomb = (await db_session.execute(select(AudioObject))).scalars().one()
    assert tomb.status == "delete_requested"
    assert tomb.delete_reason == "cancelled_upload_completion"
    assert tomb.key in meetings_enabled.blobs


async def test_a_completion_landing_between_head_and_abort_is_reconciled(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The INTERLEAVING, not just "completed before we looked" (issue #37 r15).

    Probe-then-abort is racy on its own: a stale completion can land between the
    HEAD and the abort. The abort then reports "already gone", and reading that
    as "nothing was there" deletes the session and recording — the only rows
    that name the key — leaving a completed object nothing can reach, not even
    the multipart reconciler, which only looks at keys some row still claims.
    """
    from sqlalchemy import select

    from app.api.v1.routes import meetings as meetings_routes
    from app.models.audio_object import AudioObject

    wid = await _make_workspace(client, user_token_headers)
    sid, _manifest = await _open_and_fill_upload(client, user_token_headers, wid)
    upload_id = next(iter(meetings_enabled.multiparts))
    key = meetings_enabled.multipart_keys[upload_id]

    # The HEAD says absent; the completion lands before the abort runs.
    def _absent_then_complete(probe_key: str) -> dict:
        # Restore normal behaviour and let the "dead" owner finish its upload.
        monkeypatch.setattr(meetings_enabled, "head", _real_head)
        meetings_enabled.complete_multipart(key, upload_id, [(1, "etag-1")])
        raise KeyError(probe_key)

    _real_head = meetings_enabled.head
    monkeypatch.setattr(meetings_enabled, "head", _absent_then_complete)
    monkeypatch.setattr(meetings_routes, "COMPLETION_LEASE_SECONDS", 0)

    cancelled = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert cancelled.status_code == 204, cancelled.text

    # The object exists — and something still points at it, queued for
    # confirmed deletion rather than orphaned.
    assert key in meetings_enabled.blobs
    tomb = (await db_session.execute(select(AudioObject))).scalars().one()
    assert tomb.key == key
    assert tomb.status == "delete_requested"
    assert tomb.delete_reason == "cancelled_upload_completion"


async def test_a_part_landing_after_the_cancel_abort_is_not_left_behind(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation runs the same late-part check as erasure (r17).

    A part request already in flight can land after the abort, leaving parts
    stored under an upload nothing owns any more — the session row is gone with
    the recording, so the multipart reconciler will not touch the key either.
    When the parts cannot be cleared, cancellation hands the key to the deletion
    pipeline instead of reporting a clean discard.
    """
    from sqlalchemy import select

    from app.models.audio_object import AudioObject

    wid = await _make_workspace(client, user_token_headers)
    sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)
    upload_id = next(iter(meetings_enabled.multiparts))

    # Every abort is followed by a part landing: the writer never settles.
    original_abort = meetings_enabled.abort_multipart

    def _abort_then_late_part(key: str, uid: str) -> bool:
        result = original_abort(key, uid)
        meetings_enabled.multiparts[uid] = {2: b"late"}
        return result

    monkeypatch.setattr(meetings_enabled, "abort_multipart", _abort_then_late_part)

    cancelled = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert cancelled.status_code == 204, cancelled.text
    assert len(meetings_enabled.aborted) >= 2  # it tried again
    # The key is now owned by a durable tombstone, whose deletion sweep runs the
    # fenced abort/verify loop until the parts are provably gone.
    tomb = (await db_session.execute(select(AudioObject))).scalars().one()
    assert tomb.key == meetings_enabled.multipart_keys[upload_id]
    assert tomb.status == "delete_requested"


async def test_a_clean_cancel_leaves_no_tombstone(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """The ordinary path stays clean: a live upload is simply discarded.

    Tombstoning every cancellation would work, but it would also push a
    confirmed-deletion cycle through the sweep for keys that provably hold
    nothing — and would hide a regression in the "already gone" branch.
    """
    from sqlalchemy import select

    from app.models.audio_object import AudioObject

    wid = await _make_workspace(client, user_token_headers)
    sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)

    cancelled = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert cancelled.status_code == 204, cancelled.text
    assert len(meetings_enabled.aborted) == 1
    assert (await db_session.execute(select(AudioObject))).scalars().all() == []


async def test_cancel_destroys_nothing_when_the_store_is_indeterminate(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout/5xx says NOTHING about whether the object exists.

    Guessing "absent" would abort an upload that may have completed and delete
    the recording that owns it. The request refuses instead — and hands the
    claim straight back, so the retry is not told the session is busy for the
    whole lease (issue #37 review round 14).
    """
    wid = await _make_workspace(client, user_token_headers)
    sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)

    def _unavailable(key: str):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(meetings_enabled, "head", _unavailable)
    refused = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert refused.status_code == 503, refused.text
    assert meetings_enabled.aborted == []
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert len(listed.json()) == 1  # the recording shell survives

    # The claim was RELEASED: the immediate retry works once the store answers,
    # rather than being refused as "being completed" for two minutes.
    monkeypatch.setattr(
        meetings_enabled, "head", lambda key: (_ for _ in ()).throw(KeyError(key))
    )
    retried = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert retried.status_code == 204, retried.text
    assert meetings_enabled.aborted != []


async def test_cancel_is_idempotent(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """A client that cancels twice — or retries a cancel whose response it never
    saw — must not be told the second one failed (issue #37 review r12)."""
    wid = await _make_workspace(client, user_token_headers)
    sid, _ = await _open_and_fill_upload(client, user_token_headers, wid)

    first = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert first.status_code == 204, first.text
    second = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert second.status_code == 204, second.text
    # Exactly one store-side abort, and no orphaned recording shell.
    assert len(meetings_enabled.aborted) == 1
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert listed.json() == []


async def test_cancel_reclaims_a_dead_completion(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completion whose owner died must not make the upload permanently
    un-cancellable: once its lease goes stale, cancel reclaims and cleans up."""
    wid = await _make_workspace(client, user_token_headers)
    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)

    def _die(key: str, upload_id: str, parts) -> None:
        raise RuntimeError("owner died mid-completion")

    monkeypatch.setattr(meetings_enabled, "complete_multipart", _die)
    with pytest.raises(RuntimeError):
        await client.post(
            f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
            headers=user_token_headers,
            json=manifest,
        )

    # While the lease is fresh, cancel is refused...
    held = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert held.status_code == 409, held.text

    # ...and once it goes stale, cancel takes the session and cleans up.
    from app.api.v1.routes import meetings as meetings_routes

    monkeypatch.setattr(meetings_routes, "COMPLETION_LEASE_SECONDS", 0)
    done = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}", headers=user_token_headers
    )
    assert done.status_code == 204, done.text
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert listed.json() == []


async def test_completion_recovers_when_the_owner_failed_BEFORE_storing(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completion that dies before the store accepts the object must not
    strand the session (issue #37 review round 10).

    The claim is a LEASE: once it expires another attempt reclaims it, and the
    finalize is idempotent, so the retry actually finishes the upload instead of
    being told "still in flight" forever.
    """
    wid = await _make_workspace(client, user_token_headers)
    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)

    original_complete = meetings_enabled.complete_multipart

    def _boom(*_a, **_k):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(meetings_enabled, "complete_multipart", _boom)
    with pytest.raises(RuntimeError):
        await client.post(
            f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
            headers=user_token_headers,
            json=manifest,
        )

    # The store recovers, and the lease is stale (clock moved past it). Restore
    # only this attribute — monkeypatch.undo() would also revert the fixture's
    # feature flags and turn meetings off.
    monkeypatch.setattr(meetings_enabled, "complete_multipart", original_complete)
    from app.api.v1.routes import meetings as meetings_routes

    monkeypatch.setattr(meetings_routes, "COMPLETION_LEASE_SECONDS", 0)

    retried = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "pending"
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert len(listed.json()) == 1


async def test_completion_recovers_when_the_owner_failed_AFTER_storing(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dangerous ordering: the store ACCEPTED the object, then head()
    failed. No attempt/outbox row was created, so the object would sit there
    until the expiry sweep deleted it as an orphan (issue #37 r10).

    The retry must FINALIZE the existing object rather than re-run (or abandon)
    the multipart completion.
    """
    wid = await _make_workspace(client, user_token_headers)
    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)

    original_head = meetings_enabled.head
    calls = {"complete": 0, "head": 0}
    original_complete = meetings_enabled.complete_multipart

    def _complete_then_fail_head(key, upload_id, parts):
        calls["complete"] += 1
        original_complete(key, upload_id, parts)  # the object IS stored

    def _head_absent_then_transient(key: str):
        calls["head"] += 1
        if calls["head"] == 1:
            raise KeyError(key)  # genuinely absent: proceed to complete
        raise RuntimeError("transient head failure")  # stored, but unverified

    monkeypatch.setattr(
        meetings_enabled, "complete_multipart", _complete_then_fail_head
    )
    monkeypatch.setattr(meetings_enabled, "head", _head_absent_then_transient)
    with pytest.raises(RuntimeError):
        await client.post(
            f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
            headers=user_token_headers,
            json=manifest,
        )
    assert calls["complete"] == 1

    # head() recovers; the lease is stale so the retry may reclaim.
    monkeypatch.setattr(meetings_enabled, "head", original_head)
    from app.api.v1.routes import meetings as meetings_routes

    monkeypatch.setattr(meetings_routes, "COMPLETION_LEASE_SECONDS", 0)

    retried = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "pending"
    # The ALREADY-STORED object was finalized, not completed a second time.
    assert calls["complete"] == 1
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert len(listed.json()) == 1


async def test_unknown_head_never_repeats_the_external_completion(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An UNAVAILABLE head() says nothing about whether the object exists, so it
    must not be read as absence (issue #37 review round 11).

    Treating a timeout/403/5xx as "not there" would re-run the multipart
    completion over an object the store may already hold. The request stays
    retryable instead — without repeating the side effect.
    """
    wid = await _make_workspace(client, user_token_headers)
    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)

    calls = {"complete": 0}
    original_complete = meetings_enabled.complete_multipart
    original_head = meetings_enabled.head

    def _count(key, upload_id, parts):
        calls["complete"] += 1
        original_complete(key, upload_id, parts)

    def _head_unavailable(key: str):
        raise RuntimeError("store 503")

    monkeypatch.setattr(meetings_enabled, "complete_multipart", _count)
    monkeypatch.setattr(meetings_enabled, "head", _head_unavailable)

    # Two attempts, both with head() down and the lease already stale.
    from app.api.v1.routes import meetings as meetings_routes

    monkeypatch.setattr(meetings_routes, "COMPLETION_LEASE_SECONDS", 0)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await client.post(
                f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
                headers=user_token_headers,
                json=manifest,
            )
    # The external completion was NEVER attempted on an unknown probe.
    assert calls["complete"] == 0

    # Once the store answers again, the retry finishes normally. (Restore only
    # this attribute — monkeypatch.undo() would revert the fixture's feature
    # flags too and the endpoint would 503.)
    monkeypatch.setattr(meetings_enabled, "head", original_head)
    done = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert done.status_code == 202, done.text


async def test_part_upload_after_completion_keeps_its_contract(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """A part sent after completion is a conflict, not a recording: this
    endpoint's response model is dict[str, int], so returning a recording here
    would fail response validation as a 500 (issue #37 review r9)."""
    wid = await _make_workspace(client, user_token_headers)
    sid, manifest = await _open_and_fill_upload(client, user_token_headers, wid)
    done = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/complete",
        headers=user_token_headers,
        json=manifest,
    )
    assert done.status_code == 202, done.text

    late = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sid}/parts/2",
        headers={
            **user_token_headers,
            "X-Part-Sha256": hashlib.sha256(b"x").hexdigest(),
        },
        content=b"x",
    )
    assert late.status_code == 409
    assert "no longer active" in late.json()["detail"]


async def test_direct_upload_stores_client_diagnostics(
    client: AsyncClient,
    db_session: AsyncSession,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """The file-picker path uses the DIRECT route, so it must persist the same
    closed-token diagnostics as the resumable one (issue #37 review r9)."""
    from sqlalchemy import select

    wid = await _make_workspace(client, user_token_headers)
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        data={
            "title": "Picked",
            "participant_consent": "true",
            "client_diagnostics": json.dumps(
                {
                    "browser_family": "Firefox",
                    "browser_major": 128,
                    "container": "webm",
                    "codec": "opus",
                    "total_bytes": 16,
                }
            ),
        },
    )
    assert resp.status_code == 202, resp.text
    rec = (
        await db_session.execute(
            select(MeetingRecording).where(
                MeetingRecording.id == uuid.UUID(resp.json()["id"])
            )
        )
    ).scalar_one()
    await db_session.refresh(rec)
    assert rec.client_diagnostics["browser_family"] == "Firefox"
    assert rec.client_diagnostics["container"] == "webm"

    # Hostile/malformed diagnostics are REFUSED, not silently dropped.
    bad = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        data={
            "title": "Hostile",
            "participant_consent": "true",
            "client_diagnostics": json.dumps({"container": "secret/topsecret"}),
        },
    )
    assert bad.status_code == 422


async def test_client_event_allows_upload_only_members(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """An UPLOAD-only member hits the picker preflight, so their diagnostics
    must be accepted — RECORD and UPLOAD are independently grantable and the
    endpoint serves both surfaces (issue #37 r8)."""
    wid = await _make_workspace(client, admin_token_headers, name="upload-only")
    added = await client.post(
        f"{API}/workspaces/{wid}/members",
        headers=admin_token_headers,
        json={"user_id": str(normal_user.id), "role": "editor"},
    )
    assert added.status_code in (200, 201), added.text
    # Withhold RECORD from editors — leaving UPLOAD, which they still have.
    revoked = await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=admin_token_headers,
        json={"capability": "record", "allow": False, "role": "editor"},
    )
    assert revoked.status_code in (200, 201), revoked.text

    resp = await client.post(
        f"{API}/workspaces/{wid}/meetings/client-events",
        headers=user_token_headers,
        json={"event": "preflight_bad_header", "container": "webm"},
    )
    assert resp.status_code == 202, resp.text


async def test_client_event_requires_the_recording_capability(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """A read-only member must not be able to forge recorder telemetry: the
    endpoint carries the same boundary as recording itself (issue #37 r5)."""
    wid = await _make_workspace(client, admin_token_headers, name="owned")
    # Add the normal user as a VIEWER (read access, no recording capability).
    resp = await client.post(
        f"{API}/workspaces/{wid}/members",
        headers=admin_token_headers,
        json={"user_id": str(normal_user.id), "role": "viewer"},
    )
    assert resp.status_code in (200, 201), resp.text

    forged = await client.post(
        f"{API}/workspaces/{wid}/meetings/client-events",
        headers=user_token_headers,
        # A payload that passes schema validation, so the 403 proves the
        # CAPABILITY gate rejected it rather than the body shape.
        json={"event": "preflight_empty", "browser_family": "Chrome"},
    )
    assert forged.status_code == 403


async def test_client_event_requires_workspace_access(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """The diagnostics endpoint is workspace-authorized like every other
    meetings route — it is callable by the browser, so it must not be a hole."""
    other = await _make_workspace(client, admin_token_headers, name="admins")
    resp = await client.post(
        f"{API}/workspaces/{other}/meetings/client-events",
        headers=user_token_headers,
        json={"event": "preflight_empty"},
    )
    assert resp.status_code in (403, 404)


async def test_config_reports_meetings_service_status(
    client: AsyncClient,
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "STT_BASE_URL", "http://stt.example/v1")
    cfg = await client.get(f"{API}/config")
    assert cfg.json()["meetings_status"] == "available"

    monkeypatch.setattr(
        dependency_health, "_snapshot_consumed_queue_names", lambda: frozenset()
    )
    dependency_health._reset_queue_snapshot_cache()
    cfg = await client.get(f"{API}/config")
    assert cfg.json()["meetings_status"] == "degraded"


# ------------------------------------------- resumable multipart uploads (B1)


async def _open_session(client: AsyncClient, headers: dict[str, str], wid: str) -> dict:
    resp = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads",
        headers=headers,
        json={"title": "Big meeting", "filename": "m.webm"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_multipart_upload_session_end_to_end(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    import hashlib as _hashlib

    from sqlalchemy import select

    from app.models.meeting_transcription_attempt import MeetingTranscriptionAttempt
    from app.models.task_outbox import TaskOutbox

    wid = await _make_workspace(client, user_token_headers)
    sess = await _open_session(client, user_token_headers, wid)
    assert sess["part_size_bytes"] > 0

    parts = {1: b"chunk-one-", 2: b"chunk-two"}
    manifest = []
    for number, data in parts.items():
        digest = _hashlib.sha256(data).hexdigest()
        up = await client.put(
            f"{API}/workspaces/{wid}/meetings/uploads/{sess['id']}/parts/{number}",
            headers={**user_token_headers, "X-Part-Sha256": digest},
            content=data,
        )
        assert up.status_code == 200, up.text
        manifest.append({"part_number": number, "sha256": digest})

    done = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sess['id']}/complete",
        headers=user_token_headers,
        json={"parts": manifest},
    )
    assert done.status_code == 202, done.text
    rec = done.json()
    assert rec["status"] == "pending"
    assert rec["size_bytes"] == len(b"chunk-one-chunk-two")
    # Assembled object + transactional attempt/outbox (the wave-A2 guarantee).
    key = next(iter(meetings_enabled.blobs))
    assert meetings_enabled.blobs[key] == b"chunk-one-chunk-two"
    attempt = (
        (await db_session.execute(select(MeetingTranscriptionAttempt))).scalars().one()
    )
    assert attempt.status == "pending"
    assert (await db_session.execute(select(TaskOutbox))).scalars().one()


async def test_part_checksum_mismatch_is_retryable_422(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    sess = await _open_session(client, user_token_headers, wid)
    bad = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sess['id']}/parts/1",
        headers={**user_token_headers, "X-Part-Sha256": "0" * 64},
        content=b"corrupted-in-flight",
    )
    assert bad.status_code == 422
    # The client re-sends the SAME part after the mismatch — idempotent upsert.
    import hashlib as _hashlib

    data = b"correct-bytes"
    ok = await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sess['id']}/parts/1",
        headers={
            **user_token_headers,
            "X-Part-Sha256": _hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    assert ok.status_code == 200


async def test_complete_rejects_mismatched_manifest(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    import hashlib as _hashlib

    wid = await _make_workspace(client, user_token_headers)
    sess = await _open_session(client, user_token_headers, wid)
    data = b"only-part"
    await client.put(
        f"{API}/workspaces/{wid}/meetings/uploads/{sess['id']}/parts/1",
        headers={
            **user_token_headers,
            "X-Part-Sha256": _hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    # Manifest claims a part that was never uploaded.
    bad = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads/{sess['id']}/complete",
        headers=user_token_headers,
        json={
            "parts": [
                {"part_number": 1, "sha256": _hashlib.sha256(data).hexdigest()},
                {"part_number": 2, "sha256": "0" * 64},
            ]
        },
    )
    assert bad.status_code == 409


async def test_abort_upload_session_is_a_true_cancel(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    sess = await _open_session(client, user_token_headers, wid)
    gone = await client.delete(
        f"{API}/workspaces/{wid}/meetings/uploads/{sess['id']}",
        headers=user_token_headers,
    )
    assert gone.status_code == 204
    # S3 upload aborted (parts discarded), recording row removed.
    assert meetings_enabled.aborted == ["mp-1"]
    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings", headers=user_token_headers
    )
    assert listed.json() == []


# ------------------------------------------------ Range playback proxy (B2)


async def test_range_playback_206_416_and_head(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"0123456789" * 10  # 100 bytes
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    from app.models.audio_object import AudioObject

    db_session.add(
        AudioObject(
            bucket="b",
            key="k1",
            kind="meeting_recording",
            ref_id=rec.id,
            size_bytes=100,
            etag="etag-100",
        )
    )
    await db_session.commit()
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}/audio"

    # Full GET: 200 with length + Accept-Ranges + ETag.
    full = await client.get(base, headers=user_token_headers)
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == "100"
    assert full.headers["etag"] == '"etag-100"'

    # Seek: 206 with the exact span (never the whole object).
    partial = await client.get(
        base, headers={**user_token_headers, "Range": "bytes=10-19"}
    )
    assert partial.status_code == 206
    assert partial.headers["content-range"] == "bytes 10-19/100"
    assert partial.content == b"0123456789"

    # Unsatisfiable: 416 with the star form.
    beyond = await client.get(
        base, headers={**user_token_headers, "Range": "bytes=500-"}
    )
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == "bytes */100"

    # Stale If-Range validator falls back to a coherent full 200.
    stale = await client.get(
        base,
        headers={
            **user_token_headers,
            "Range": "bytes=10-19",
            "If-Range": '"old-etag"',
        },
    )
    assert stale.status_code == 200

    # HEAD advertises the metadata without a body.
    head = await client.head(base, headers=user_token_headers)
    assert head.status_code == 200
    assert head.headers["content-length"] == "100"
    assert head.content == b""


async def test_playback_token_flow_and_download(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio-bytes"
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    # Mint a playback URL (authz happens here); the token then streams WITHOUT
    # an Authorization header — exactly what <audio src> needs.
    minted = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/session",
        headers=user_token_headers,
    )
    assert minted.status_code == 200, minted.text
    url = minted.json()["url"]
    played = await client.get(url)
    assert played.status_code == 200
    assert played.content == b"audio-bytes"

    # A token for one recording opens nothing else, and garbage is refused.
    other = await client.get(
        f"{API}/workspaces/{wid}/recordings/{uuid.uuid4()}/audio",
        params={"token": url.split("token=")[1]},
    )
    assert other.status_code == 401
    junk = await client.get(url.split("?")[0], params={"token": "garbage"})
    assert junk.status_code == 401

    # Download is its own authorized surface with Content-Disposition.
    dl = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/download",
        headers=user_token_headers,
    )
    assert dl.status_code == 200
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.headers["content-disposition"].endswith('.webm"')


# ------------------------------------------------ consent policy (issue #6, B5)


async def test_consent_policy_gates_recording_and_stamps_evidence(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MEETING_REQUIRE_CONSENT", True)
    wid = await _make_workspace(client, user_token_headers)

    # Without the attestation: refused server-side (the UI checkbox alone
    # gated nothing before #6).
    refused = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"bytes", "audio/webm")},
        data={"title": "No consent"},
    )
    assert refused.status_code == 400
    assert "consent" in refused.json()["detail"].lower()

    refused_session = await client.post(
        f"{API}/workspaces/{wid}/meetings/uploads",
        headers=user_token_headers,
        json={"title": "No consent", "filename": "m.webm"},
    )
    assert refused_session.status_code == 400

    # With it: accepted, and the attestation evidence is recorded.
    accepted = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=user_token_headers,
        files={"file": ("m.webm", b"bytes", "audio/webm")},
        data={"title": "Consented", "participant_consent": "true"},
    )
    assert accepted.status_code == 202, accepted.text
    rec = await db_session.get(MeetingRecording, uuid.UUID(accepted.json()["id"]))
    assert rec.consent_policy_version == settings.CONSENT_POLICY_VERSION
    assert rec.consent_method == "upload_attestation"
    assert rec.consent_actor_email is not None
    assert rec.consent_recorded_at is not None


@pytest.mark.parametrize("requested_language", [None, "auto", "en", "sv", "invalid"])
@pytest.mark.parametrize("resumable", [False, True])
async def test_recording_language_persisted_and_validated(
    client,
    user_token_headers,
    meetings_enabled,
    db_session,
    requested_language,
    resumable,
):
    wid = await _make_workspace(client, user_token_headers)
    data = {"title": "Language choice", "participant_consent": True}
    if requested_language is not None:
        data["requested_language"] = requested_language
    if resumable:
        response = await client.post(
            f"{API}/workspaces/{wid}/meetings/uploads",
            headers=user_token_headers,
            json={**data, "filename": "m.webm"},
        )
    else:
        response = await client.post(
            f"{API}/workspaces/{wid}/recordings",
            headers=user_token_headers,
            data=data,
            files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        )
    if requested_language == "invalid":
        assert response.status_code == 422
        assert not meetings_enabled.blobs
        assert not meetings_enabled.multiparts
        return
    assert response.status_code == (201 if resumable else 202), response.text
    rid = response.json()["recording_id" if resumable else "id"]
    recording = await db_session.get(MeetingRecording, uuid.UUID(rid))
    assert recording.requested_language == (requested_language or "auto")
    # User preference must not pretend to be the model's detected language.
    assert recording.language is None
    detail = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rid}", headers=user_token_headers
    )
    assert detail.json()["requested_language"] == (requested_language or "auto")


@pytest.mark.parametrize("expected_speakers", [None, 2, 10, 0, 11])
@pytest.mark.parametrize("resumable", [False, True])
async def test_expected_speakers_persisted_and_validated(
    client,
    user_token_headers,
    meetings_enabled,
    db_session,
    expected_speakers,
    resumable,
):
    wid = await _make_workspace(client, user_token_headers)
    data = {"title": "Two of us", "participant_consent": True}
    if expected_speakers is not None:
        data["expected_speakers"] = expected_speakers
    if resumable:
        response = await client.post(
            f"{API}/workspaces/{wid}/meetings/uploads",
            headers=user_token_headers,
            json={**data, "filename": "m.webm"},
        )
    else:
        response = await client.post(
            f"{API}/workspaces/{wid}/recordings",
            headers=user_token_headers,
            data=data,
            files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        )
    if expected_speakers in (0, 11):
        assert response.status_code == 422
        assert not meetings_enabled.blobs
        assert not meetings_enabled.multiparts
        return
    assert response.status_code == (201 if resumable else 202), response.text
    rid = response.json()["recording_id" if resumable else "id"]
    recording = await db_session.get(MeetingRecording, uuid.UUID(rid))
    assert recording.expected_speakers == expected_speakers
    detail = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rid}", headers=user_token_headers
    )
    assert detail.json()["expected_speakers"] == expected_speakers


@pytest.mark.parametrize("resumable", [False, True])
async def test_audio_layout_is_stored_with_the_microphone_side_count(
    client,
    user_token_headers,
    meetings_enabled,
    db_session,
    resumable,
):
    """What the channels hold comes with the recording. The count of people at
    the microphone only means something for an online-meeting recording."""
    wid = await _make_workspace(client, user_token_headers)

    async def record(**fields):
        data = {"title": "Meet", "participant_consent": True, **fields}
        if resumable:
            return await client.post(
                f"{API}/workspaces/{wid}/meetings/uploads",
                headers=user_token_headers,
                json={**data, "filename": "m.webm"},
            )
        return await client.post(
            f"{API}/workspaces/{wid}/recordings",
            headers=user_token_headers,
            data=data,
            files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        )

    async def stored(response):
        assert response.status_code == (201 if resumable else 202), response.text
        rid = response.json()["recording_id" if resumable else "id"]
        row = await db_session.get(MeetingRecording, uuid.UUID(rid))
        detail = await client.get(
            f"{API}/workspaces/{wid}/recordings/{rid}", headers=user_token_headers
        )
        assert detail.json()["audio_layout"] == row.audio_layout
        assert detail.json()["mic_speakers"] == row.mic_speakers
        return row.audio_layout, row.mic_speakers, row.expected_speakers

    # Nothing said: one mix, as every recording so far.
    assert await stored(await record()) == ("mixed", None, None)
    # An online meeting: just me here, two people in the call.
    assert await stored(
        await record(audio_layout="mic_tab", mic_speakers=1, expected_speakers=2)
    ) == ("mic_tab", 1, 2)
    # A tab only: there is no microphone side to count.
    assert await stored(await record(audio_layout="tab", mic_speakers=3)) == (
        "tab",
        None,
        None,
    )
    for bad in (
        {"audio_layout": "surround"},
        {"mic_speakers": 0},
        {"mic_speakers": 11},
    ):
        assert (await record(**bad)).status_code == 422, bad
