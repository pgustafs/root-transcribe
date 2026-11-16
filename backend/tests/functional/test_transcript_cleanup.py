"""Functional tests: the cleaned copy of a transcript."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.meeting_transcript_segment import MeetingTranscriptSegment
from app.models.task_outbox import TaskOutbox
from app.models.transcript_cleanup import TranscriptCleanup
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


async def test_cleanup_is_queued_shown_and_decided_line_by_line(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}"
    url = f"{base}/cleanup"

    assert (await client.get(url, headers=user_token_headers)).json() is None
    # No LLM configured: refuse instead of queueing work nobody can do.
    assert (await client.post(url, headers=user_token_headers)).status_code == 503
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://llm.invalid/v1")

    resp = await client.post(url, headers=user_token_headers)
    assert resp.status_code == 202, resp.text
    cleanup_id = resp.json()["id"]
    assert resp.json()["status"] == "pending" and resp.json()["changes"] == []
    # Asking again while it works returns the same one, not a second.
    again = await client.post(url, headers=user_token_headers)
    assert again.json()["id"] == cleanup_id
    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.task_name == "tasks.clean_transcript"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [o.payload for o in outbox] == [{"cleanup_id": cleanup_id}]
    # Not removable while it is being made.
    resp = await client.delete(f"{url}/{cleanup_id}", headers=user_token_headers)
    assert resp.status_code == 409

    # The worker's result: one corrected line.
    segments = (
        (
            await db_session.execute(
                select(MeetingTranscriptSegment)
                .where(MeetingTranscriptSegment.recording_id == rec.id)
                .order_by(MeetingTranscriptSegment.idx)
            )
        )
        .scalars()
        .all()
    )
    first = str(segments[0].id)
    cleanup = await db_session.get(TranscriptCleanup, uuid.UUID(cleanup_id))
    cleanup.status = "ready"
    cleanup.rejected = 2
    cleanup.changes = [
        {
            "segment_id": first,
            "original": "Hello everyone.",
            "text": "Hello, everyone!",
            "status": "suggested",
        }
    ]
    await db_session.commit()

    shown = (await client.get(url, headers=user_token_headers)).json()
    assert shown["status"] == "ready" and shown["rejected"] == 2
    assert [(c["segment_id"], c["text"], c["status"]) for c in shown["changes"]] == [
        (first, "Hello, everyone!", "suggested")
    ]
    # The transcript is untouched by a cleanup.
    detail = (await client.get(base, headers=user_token_headers)).json()
    assert [s["text"] for s in detail["segments"]] == ["Hello everyone.", "Hi there."]

    # A person keeps the original line, then takes the correction back.
    change = f"{url}/{cleanup_id}/changes/{first}"
    for decision, status_ in (("dismiss", "dismissed"), ("use", "suggested")):
        resp = await client.post(
            change, json={"decision": decision}, headers=user_token_headers
        )
        assert resp.status_code == 200, resp.text
        assert [c["status"] for c in resp.json()["changes"]] == [status_]
    # A line without a correction, or an unknown decision.
    resp = await client.post(
        f"{url}/{cleanup_id}/changes/{segments[1].id}",
        json={"decision": "dismiss"},
        headers=user_token_headers,
    )
    assert resp.status_code == 404
    resp = await client.post(
        change, json={"decision": "apply"}, headers=user_token_headers
    )
    assert resp.status_code == 422

    # Making it again replaces what is shown; the old one can be removed.
    newer = await client.post(url, headers=user_token_headers)
    assert newer.status_code == 202 and newer.json()["id"] != cleanup_id
    assert (await client.get(url, headers=user_token_headers)).json()["id"] == (
        newer.json()["id"]
    )
    resp = await client.delete(f"{url}/{cleanup_id}", headers=user_token_headers)
    assert resp.status_code == 204
    resp = await client.delete(f"{url}/{cleanup_id}", headers=user_token_headers)
    assert resp.status_code == 404

    # Restricted recording: sending the transcript to the model needs a grant.
    rec.sensitivity = "restricted"
    await db_session.commit()
    newer_row = await db_session.get(TranscriptCleanup, uuid.UUID(newer.json()["id"]))
    newer_row.status = "ready"
    await db_session.commit()
    assert (await client.post(url, headers=user_token_headers)).status_code == 403


async def test_viewers_can_read_a_cleanup_but_not_make_or_change_one(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    power_user: User,
    user_token_headers: dict[str, str],
    power_user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://llm.invalid/v1")
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    url = f"{API}/workspaces/{wid}/recordings/{rec.id}/cleanup"
    added = await client.post(
        f"{API}/workspaces/{wid}/members",
        json={"email": power_user.email, "role": "viewer"},
        headers=user_token_headers,
    )
    assert added.status_code == 201, added.text
    made = await client.post(url, headers=user_token_headers)
    cleanup_id = made.json()["id"]
    cleanup = await db_session.get(TranscriptCleanup, uuid.UUID(cleanup_id))
    cleanup.status = "ready"
    await db_session.commit()

    read = await client.get(url, headers=power_user_token_headers)
    assert read.status_code == 200 and read.json()["id"] == cleanup_id
    for method, path, body in [
        ("post", url, None),
        ("post", f"{url}/{cleanup_id}/changes/{uuid.uuid4()}", {"decision": "dismiss"}),
        ("delete", f"{url}/{cleanup_id}", None),
    ]:
        resp = await client.request(
            method, path, json=body, headers=power_user_token_headers
        )
        assert resp.status_code == 403, (method, path, resp.text)
