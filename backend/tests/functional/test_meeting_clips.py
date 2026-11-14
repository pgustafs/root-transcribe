"""Functional tests for governed audio clips (#6, wave D5)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audio_object import AudioObject
from app.models.meeting_clip import MeetingClip
from app.models.task_outbox import TaskOutbox
from app.models.user import User

from .test_meetings_api import (
    _FakeAudioStore,
    _make_workspace,
    _seed_completed_recording,
    meetings_enabled,
)

pytestmark = pytest.mark.functional

API = "/api/v1"

__all__ = ["meetings_enabled"]  # re-exported fixture


async def test_clip_create_is_durable_and_gated(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}/clips"

    # Invalid range is rejected before any row exists.
    resp = await client.post(
        base,
        json={"title": "Bad", "start_ms": 5000, "end_ms": 5000},
        headers=user_token_headers,
    )
    assert resp.status_code == 422

    resp = await client.post(
        base,
        json={"title": "Highlight", "start_ms": 1000, "end_ms": 4000},
        headers=user_token_headers,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"

    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.task_name == "tasks.create_meeting_clip"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(outbox) == 1
    assert outbox[0].payload == {"clip_id": body["id"]}
    assert outbox[0].queue == "transcription"

    resp = await client.get(base, headers=user_token_headers)
    assert resp.status_code == 200
    assert [c["title"] for c in resp.json()] == ["Highlight"]

    # Restricted recording: create_clip needs an explicit user grant (E2).
    rec.sensitivity = "restricted"
    await db_session.commit()
    resp = await client.post(
        base,
        json={"title": "Nope", "start_ms": 0, "end_ms": 1000},
        headers=user_token_headers,
    )
    assert resp.status_code == 403


async def test_clip_playback_and_hold_aware_delete(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    from app.models.meeting_governance import LegalHold

    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    # Seed a ready clip as the worker would have left it.
    key = f"workspaces/{wid}/clips/abc.webm"
    meetings_enabled.blobs[key] = b"0123456789"
    clip = MeetingClip(
        workspace_id=uuid.UUID(wid),
        recording_id=rec.id,
        title="Ready clip",
        start_ms=0,
        end_ms=2000,
        status="ready",
        audio_key=key,
        size_bytes=10,
        created_by_id=normal_user.id,
    )
    db_session.add(clip)
    db_session.add(
        AudioObject(bucket="b", key=key, kind="clip", ref_id=clip.id, size_bytes=10)
    )
    await db_session.commit()

    # Token URL → Range-capable proxy.
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/clips/{clip.id}/audio/session",
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    resp = await client.get(url, headers={"Range": "bytes=2-5"})
    assert resp.status_code == 206
    assert resp.content == b"2345"
    assert resp.headers["Content-Range"] == "bytes 2-5/10"

    # A legal hold on the recording blocks clip deletion.
    hold = LegalHold(scope_type="recording", scope_id=rec.id, reason="litigation")
    db_session.add(hold)
    await db_session.commit()
    resp = await client.delete(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/clips/{clip.id}",
        headers=user_token_headers,
    )
    assert resp.status_code == 409
    assert "legal_hold_active" in resp.text

    # Released hold → delete tombstones the audio object (honest lifecycle).
    hold.released_at = hold.created_at
    await db_session.commit()
    resp = await client.delete(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/clips/{clip.id}",
        headers=user_token_headers,
    )
    assert resp.status_code == 204
    obj = (
        (await db_session.execute(select(AudioObject).where(AudioObject.key == key)))
        .scalars()
        .one()
    )
    assert obj.status == "delete_requested"
    assert meetings_enabled.blobs.get(key) == b"0123456789"  # not touched inline
    clips = (await db_session.execute(select(MeetingClip))).scalars().all()
    assert clips == []
