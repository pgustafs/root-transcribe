"""Functional tests: naming speakers by hand and from the transcript."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crud import transcript_revision as transcript_revision_crud
from app.models.meeting_transcript_segment import MeetingTranscriptSegment
from app.models.speaker_name_run import SpeakerNameRun
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


async def _names(
    client: AsyncClient, base: str, headers: dict
) -> dict[str, str | None]:
    segments = (await client.get(base, headers=headers)).json()["segments"]
    return {s["speaker_label"]: s["speaker_name"] for s in segments}


async def test_rename_a_speaker_by_hand(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    admin_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}"
    assign = f"{base}/assign-speaker"

    resp = await client.post(
        assign,
        json={"speaker_label": "SPEAKER_01", "speaker_name": "  Peter  "},
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"updated_segments": 1}
    assert await _names(client, base, user_token_headers) == {
        "SPEAKER_00": None,
        "SPEAKER_01": "Peter",
    }

    # Exports use the typed name.
    exported = await client.get(
        f"{base}/revisions/{rec.latest_revision_id}/export?format=txt",
        headers=user_token_headers,
    )
    assert exported.status_code == 200, exported.text
    assert "Peter: Hi there." in exported.text
    assert "SPEAKER_00: Hello everyone." in exported.text

    # A directory person replaces the typed name, and the other way round.
    bob = (
        await client.post(
            f"{API}/speakers",
            headers=admin_token_headers,
            json={"display_name": "Bob", "consent_granted": False},
        )
    ).json()["id"]
    await client.post(
        assign,
        json={"speaker_label": "SPEAKER_01", "speaker_profile_id": bob},
        headers=user_token_headers,
    )
    assert (await _names(client, base, user_token_headers))["SPEAKER_01"] == "Bob"
    await client.post(
        assign,
        json={"speaker_label": "SPEAKER_01", "speaker_name": "Robert"},
        headers=user_token_headers,
    )
    assert (await _names(client, base, user_token_headers))["SPEAKER_01"] == "Robert"
    segment = (
        await db_session.execute(
            select(MeetingTranscriptSegment).where(
                MeetingTranscriptSegment.recording_id == rec.id,
                MeetingTranscriptSegment.speaker_label == "SPEAKER_01",
            )
        )
    ).scalar_one()
    await db_session.refresh(segment)
    assert segment.speaker_profile_id is None
    assert segment.speaker_display_name == "Robert"

    # One line can be corrected on its own.
    resp = await client.patch(
        f"{base}/segments/{segment.id}",
        json={"speaker_name": "Bobby"},
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["speaker_name"] == "Bobby"

    # Clearing returns the speaker to "Speaker N"; an empty name is refused.
    await client.post(
        assign,
        json={"speaker_label": "SPEAKER_01", "clear_speaker": True},
        headers=user_token_headers,
    )
    assert (await _names(client, base, user_token_headers))["SPEAKER_01"] is None
    resp = await client.post(
        assign,
        json={"speaker_label": "SPEAKER_01", "speaker_name": "   "},
        headers=user_token_headers,
    )
    assert resp.status_code == 422


async def test_a_typed_name_follows_the_transcript_into_a_new_version(
    db_session: AsyncSession,
    client: AsyncClient,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/assign-speaker",
        json={"speaker_label": "SPEAKER_01", "speaker_name": "Peter"},
        headers=user_token_headers,
    )

    recording_id, previous = rec.id, rec.approved_revision_id

    def new_version(sync_session):
        # What a re-transcription does: new segments, then carry the edits over.
        revision = transcript_revision_crud.create_revision_sync(
            sync_session,
            recording_id=recording_id,
            attempt_id=None,
            origin="retranscription",
            seg_rows=[
                {
                    "idx": 0,
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "speaker_label": "SPEAKER_07",
                    "text": "Hi there!",
                }
            ],
        )
        transcript_revision_crud.carry_over_edits_sync(
            sync_session, revision, previous_revision_id=previous
        )
        return revision.id

    revision_id = await db_session.run_sync(new_version)
    await db_session.commit()
    segment = (
        await db_session.execute(
            select(MeetingTranscriptSegment).where(
                MeetingTranscriptSegment.revision_id == revision_id
            )
        )
    ).scalar_one()
    assert segment.speaker_display_name == "Peter"


async def test_identify_queues_a_run_and_a_person_decides(
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
    names = f"{base}/speaker-names"

    assert (await client.get(names, headers=user_token_headers)).json() is None
    # No LLM configured: refuse instead of queueing work nobody can do.
    resp = await client.post(f"{names}/identify", headers=user_token_headers)
    assert resp.status_code == 503
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://llm.invalid/v1")

    resp = await client.post(f"{names}/identify", headers=user_token_headers)
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["id"]
    assert resp.json()["status"] == "pending"
    # Asking again while it works returns the same run, not a second one.
    again = await client.post(f"{names}/identify", headers=user_token_headers)
    assert again.json()["id"] == run_id
    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.task_name == "tasks.identify_speaker_names"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [o.payload for o in outbox] == [{"run_id": run_id}]

    # The worker's result.
    run = await db_session.get(SpeakerNameRun, uuid.UUID(run_id))
    run.status = "ready"
    run.suggestions = [
        {
            "speaker_label": label,
            "name": name,
            "quote": f"my name is {name}",
            "start_ms": 0,
            "confidence": "high",
            "status": "pending",
        }
        for label, name in (("SPEAKER_00", "Anna"), ("SPEAKER_01", "Peter"))
    ]
    await db_session.commit()

    shown = (await client.get(names, headers=user_token_headers)).json()
    assert [s["name"] for s in shown["suggestions"]] == ["Anna", "Peter"]
    # Nothing is applied until someone accepts.
    assert set((await _names(client, base, user_token_headers)).values()) == {None}

    resp = await client.post(
        f"{names}/{run_id}/suggestions/1",
        json={"decision": "accept"},
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"{names}/{run_id}/suggestions/0",
        json={"decision": "dismiss"},
        headers=user_token_headers,
    )
    assert [s["status"] for s in resp.json()["suggestions"]] == [
        "dismissed",
        "accepted",
    ]
    assert await _names(client, base, user_token_headers) == {
        "SPEAKER_00": None,
        "SPEAKER_01": "Peter",
    }
    # Decided once; unknown suggestions are not found.
    for index, expected in ((1, 409), (5, 404)):
        resp = await client.post(
            f"{names}/{run_id}/suggestions/{index}",
            json={"decision": "accept"},
            headers=user_token_headers,
        )
        assert resp.status_code == expected

    # Restricted recording: sending the transcript to the model needs a grant.
    rec.sensitivity = "restricted"
    await db_session.commit()
    resp = await client.post(f"{names}/identify", headers=user_token_headers)
    assert resp.status_code == 403


async def test_viewers_cannot_name_speakers(
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
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}"
    added = await client.post(
        f"{API}/workspaces/{wid}/members",
        json={"email": power_user.email, "role": "viewer"},
        headers=user_token_headers,
    )
    assert added.status_code == 201, added.text
    for method, url, body in [
        (
            "post",
            f"{base}/assign-speaker",
            {"speaker_label": "SPEAKER_00", "speaker_name": "X"},
        ),
        ("post", f"{base}/speaker-names/identify", None),
        ("get", f"{base}/speaker-names", None),
    ]:
        kwargs = {"json": body} if body else {}
        resp = await getattr(client, method)(
            url, headers=power_user_token_headers, **kwargs
        )
        assert resp.status_code == 403, (url, resp.text)
