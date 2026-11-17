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


async def test_retry_can_change_the_number_of_speakers(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    """Speaker detection invented a third person: the user says how many
    people spoke and transcribes again. Left out, the choice is kept; null
    goes back to detecting the count."""
    from app.models.meeting_recording import MeetingRecording

    wid = await _make_workspace(client, user_token_headers)
    rec = await _create_recording(client, user_token_headers, wid)
    url = f"{API}/workspaces/{wid}/recordings/{rec['id']}/retry"

    async def finish_attempts() -> None:
        for attempt in (
            (await db_session.execute(select(MeetingTranscriptionAttempt)))
            .scalars()
            .all()
        ):
            attempt.status = "completed"
        row = await db_session.get(MeetingRecording, uuid.UUID(rec["id"]))
        row.status = "completed"
        await db_session.commit()

    async def last_config() -> dict:
        db_session.expire_all()
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
        return attempts[-1].config

    await finish_attempts()
    changed = await client.post(
        url, headers=user_token_headers, json={"expected_speakers": 2}
    )
    assert changed.status_code == 202, changed.text
    assert changed.json()["expected_speakers"] == 2
    assert (await last_config())["expected_speakers"] == 2

    # No body: the stored choice is used again.
    await finish_attempts()
    kept = await client.post(url, headers=user_token_headers)
    assert kept.status_code == 202, kept.text
    assert kept.json()["expected_speakers"] == 2

    # Null: back to detecting the count.
    await finish_attempts()
    auto = await client.post(
        url, headers=user_token_headers, json={"expected_speakers": None}
    )
    assert auto.status_code == 202, auto.text
    assert auto.json()["expected_speakers"] is None
    assert (await last_config())["expected_speakers"] is None

    await finish_attempts()
    bad = await client.post(
        url, headers=user_token_headers, json={"expected_speakers": 0}
    )
    assert bad.status_code == 422


async def test_retry_can_change_who_is_at_the_microphone(
    client: AsyncClient,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    """An online-meeting recording made as "just me" where a colleague joined
    in the room: the count at the microphone can be corrected on a re-run.
    What the channels hold cannot: that describes the file."""
    from app.models.meeting_recording import MeetingRecording

    wid = await _make_workspace(client, user_token_headers)
    rec = await _create_recording(client, user_token_headers, wid)
    url = f"{API}/workspaces/{wid}/recordings/{rec['id']}/retry"

    async def finish(layout: str | None = None) -> None:
        for attempt in (
            (await db_session.execute(select(MeetingTranscriptionAttempt)))
            .scalars()
            .all()
        ):
            attempt.status = "completed"
        row = await db_session.get(MeetingRecording, uuid.UUID(rec["id"]))
        row.status = "completed"
        if layout:
            row.audio_layout = layout
        await db_session.commit()

    # One mix: there is no microphone side, the count is not taken.
    await finish()
    mixed = await client.post(url, headers=user_token_headers, json={"mic_speakers": 2})
    assert mixed.status_code == 202, mixed.text
    assert mixed.json()["mic_speakers"] is None

    await finish("mic_tab")
    changed = await client.post(
        url,
        headers=user_token_headers,
        json={"mic_speakers": 2, "expected_speakers": 3},
    )
    assert changed.status_code == 202, changed.text
    assert (changed.json()["mic_speakers"], changed.json()["expected_speakers"]) == (
        2,
        3,
    )
    assert changed.json()["audio_layout"] == "mic_tab"
    db_session.expire_all()
    newest = (
        (
            await db_session.execute(
                select(MeetingTranscriptionAttempt).order_by(
                    MeetingTranscriptionAttempt.attempt_number.desc()
                )
            )
        )
        .scalars()
        .first()
    )
    assert newest.config["audio_layout"] == "mic_tab"
    assert newest.config["mic_speakers"] == 2

    # Left out: kept. Null: detect.
    await finish()
    kept = await client.post(url, headers=user_token_headers)
    assert kept.json()["mic_speakers"] == 2
    await finish()
    auto = await client.post(
        url, headers=user_token_headers, json={"mic_speakers": None}
    )
    assert auto.json()["mic_speakers"] is None
