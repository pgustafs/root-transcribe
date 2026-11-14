"""Functional tests for transactional transcription enqueue (issue #6, wave A2).

Creating or retrying a recording must commit the attempt + outbox rows in the
same transaction as the domain change — a broker outage can delay delivery but
can never lose the work.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.task_outbox as task_outbox_service
from app.models.meeting_transcription_attempt import MeetingTranscriptionAttempt
from app.models.task_outbox import TaskOutbox

from .test_meetings_api import _FakeAudioStore, _make_workspace, meetings_enabled

pytestmark = pytest.mark.functional

API = "/api/v1"

__all__ = ["meetings_enabled"]  # re-exported fixture


async def _create_recording(
    client: AsyncClient, headers: dict[str, str], wid: str
) -> dict:
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings",
        headers=headers,
        files={"file": ("m.webm", b"fake-audio-bytes", "audio/webm")},
        data={"title": "Atomic"},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def test_create_recording_writes_attempt_and_outbox_atomically(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fast-path delivery fails (broker down) — the request must still succeed
    # and the durable rows must exist for the dispatcher beat.
    def _broker_down(row):
        raise RuntimeError("broker down")

    monkeypatch.setattr(task_outbox_service, "deliver_outbox_row", _broker_down)

    wid = await _make_workspace(client, user_token_headers)
    rec = await _create_recording(client, user_token_headers, wid)

    attempt = (
        (await db_session.execute(select(MeetingTranscriptionAttempt))).scalars().one()
    )
    assert attempt.recording_id == uuid.UUID(rec["id"])
    assert attempt.attempt_number == 1
    assert attempt.status == "pending"

    outbox = (await db_session.execute(select(TaskOutbox))).scalars().one()
    assert outbox.status == "pending"  # NOT lost — the beat will deliver it
    assert outbox.task_name == "tasks.transcribe_attempt"
    assert outbox.queue == "transcription"
    assert outbox.payload == {"attempt_id": str(attempt.id)}
    assert outbox.dedupe_key == f"attempt:{attempt.id}:send:1"
    assert outbox.headers and "request_id" in outbox.headers


async def test_create_recording_fast_path_marks_sent(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    await _create_recording(client, user_token_headers, wid)
    outbox = (await db_session.execute(select(TaskOutbox))).scalars().one()
    assert outbox.status == "sent"
    assert outbox.sent_at is not None


async def test_retry_creates_second_attempt_with_outbox(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _create_recording(client, user_token_headers, wid)

    # Simulate the first attempt failing.
    attempt = (
        (await db_session.execute(select(MeetingTranscriptionAttempt))).scalars().one()
    )
    attempt.status = "failed"
    from app.models.meeting_recording import MeetingRecording

    row = await db_session.get(MeetingRecording, uuid.UUID(rec["id"]))
    row.status = "failed"
    await db_session.commit()

    retried = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec['id']}/retry",
        headers=user_token_headers,
    )
    assert retried.status_code == 202, retried.text

    attempts = (
        (
            await db_session.execute(
                select(MeetingTranscriptionAttempt).order_by(
                    MeetingTranscriptionAttempt.attempt_number
                )
            )
        )
        .scalars()
        .all()
    )
    assert [a.attempt_number for a in attempts] == [1, 2]
    assert attempts[1].status == "pending"
    # The retry keeps the original container extension, not a hardcoded .webm.
    assert attempts[1].config["filename"].endswith(".webm")
    outboxes = (await db_session.execute(select(TaskOutbox))).scalars().all()
    assert len(outboxes) == 2
