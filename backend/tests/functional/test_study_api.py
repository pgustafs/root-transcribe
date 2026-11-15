"""Functional tests: study notebooks, notes and export."""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audio_object import AudioObject
from app.models.study import StudyNote
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
PNG = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + (640).to_bytes(4, "big")
    + (360).to_bytes(4, "big")
    + b"rest"
)
WAV = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 60

__all__ = ["meetings_enabled"]  # re-exported fixture


async def _notebook(client, wid, headers, **body) -> dict:
    resp = await client.post(
        f"{API}/workspaces/{wid}/study/notebooks", headers=headers, json=body
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _start(client, wid, headers, notebook_id, **body) -> str:
    session_id = str(uuid.uuid4())
    resp = await client.put(
        f"{API}/workspaces/{wid}/study/sessions/{session_id}",
        headers=headers,
        json={"notebook_id": notebook_id, **body},
    )
    assert resp.status_code == 200, resp.text
    return session_id


async def _put_note(
    client, wid, headers, session_id, seq, *, audio=True, note_id=None, **extra
):
    note_id = note_id or str(uuid.uuid4())
    data = {"session_id": session_id, "seq": str(seq), "captured_ms": "522000"}
    data |= extra
    files = {"image": ("frame.png", PNG, "image/png")}
    if audio:
        data |= {"audio_start_ms": "507000", "audio_end_ms": "543000"}
        files["audio"] = ("note.wav", WAV, "audio/wav")
    resp = await client.put(
        f"{API}/workspaces/{wid}/study/notes/{note_id}",
        headers=headers,
        data=data,
        files=files,
    )
    return note_id, resp


async def test_one_notebook_per_video_whatever_the_link_looks_like(
    client: AsyncClient, user_token_headers: dict, meetings_enabled: _FakeAudioStore
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    first = await _notebook(
        client,
        wid,
        user_token_headers,
        title="Query plans",
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=40s&list=PL1",
    )
    again = await _notebook(
        client,
        wid,
        user_token_headers,
        title="Something else",
        source_url="https://youtu.be/dQw4w9WgXcQ?si=tracking",
    )
    assert again["id"] == first["id"] and again["title"] == "Query plans"
    # The learner's own link is kept as pasted.
    assert first["source_url"].endswith("&t=40s&list=PL1")
    # Same title, another video: a separate notebook. Same title, no link: too.
    other = await _notebook(
        client,
        wid,
        user_token_headers,
        title="Query plans",
        source_url="https://www.youtube.com/watch?v=AAAAAAAAAAA",
    )
    unnamed = await _notebook(client, wid, user_token_headers, title="Query plans")
    assert len({first["id"], other["id"], unnamed["id"]}) == 3
    listed = await client.get(
        f"{API}/workspaces/{wid}/study/notebooks", headers=user_token_headers
    )
    assert len(listed.json()) == 3
    bad = await client.post(
        f"{API}/workspaces/{wid}/study/notebooks",
        headers=user_token_headers,
        json={"title": "x", "source_url": "javascript:alert(1)"},
    )
    assert bad.status_code == 422


async def test_notes_are_idempotent_ordered_and_exported(
    client: AsyncClient,
    user_token_headers: dict,
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    notebook = await _notebook(client, wid, user_token_headers, title="Query plans")
    session_id = await _start(
        client, wid, user_token_headers, notebook["id"], language="sv"
    )
    # Uploads can arrive out of order; capture order is what counts.
    second, resp = await _put_note(client, wid, user_token_headers, session_id, 1)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending"
    assert (resp.json()["image_width"], resp.json()["image_height"]) == (640, 360)
    first, resp = await _put_note(
        client, wid, user_token_headers, session_id, 0, audio=False
    )
    assert resp.json()["status"] == "screenshot_only"
    # The same note again: nothing new is stored or queued.
    _, repeat = await _put_note(
        client, wid, user_token_headers, session_id, 1, note_id=second
    )
    assert repeat.status_code == 200 and repeat.json()["id"] == second
    outbox = (await db_session.execute(select(TaskOutbox))).scalars().all()
    assert [row.task_name for row in outbox] == ["tasks.transcribe_study_note"]
    assert len(meetings_enabled.blobs) == 3  # two images, one excerpt

    # The second note finishes; the first stays a screenshot.
    note = await db_session.get(StudyNote, uuid.UUID(second))
    note.status, note.text = "ready", "Indexet används här."
    await db_session.commit()
    detail = await client.get(
        f"{API}/workspaces/{wid}/study/notebooks/{notebook['id']}",
        headers=user_token_headers,
    )
    assert [n["id"] for n in detail.json()["notes"]] == [first, second]
    assert detail.json()["note_count"] == 2

    image = await client.get(
        f"{API}/workspaces/{wid}/study/notes/{first}/image", headers=user_token_headers
    )
    assert image.status_code == 200 and image.content == PNG

    # The owner's own words: saved apart from the transcript, and removable.
    note_url = f"{API}/workspaces/{wid}/study/notes/{first}"
    written = await client.patch(
        note_url, headers=user_token_headers, json={"comment": " Compare plans. "}
    )
    assert written.status_code == 200
    assert written.json()["comment"] == "Compare plans."
    assert written.json()["text"] is None
    other = await client.patch(
        f"{API}/workspaces/{wid}/study/notes/{second}",
        headers=user_token_headers,
        json={"comment": "x"},
    )
    cleared = await client.patch(
        f"{API}/workspaces/{wid}/study/notes/{second}",
        headers=user_token_headers,
        json={"comment": ""},
    )
    assert other.json()["comment"] == "x" and cleared.json()["comment"] is None

    export = await client.get(
        f"{API}/workspaces/{wid}/study/notebooks/{notebook['id']}/export",
        headers=user_token_headers,
    )
    assert export.status_code == 200
    assert export.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        assert archive.namelist() == [
            "Query-plans.md",
            "images/note-001.png",
            "images/note-002.png",
        ]
        document = archive.read("Query-plans.md").decode()
    assert "*Screenshot only. Tab audio was unavailable.*" in document
    assert "> Indexet används här." in document
    assert "**My thoughts**\n\nCompare plans.\n" in document
    assert document.index("note-001") < document.index("note-002")

    # Wrong files are refused.
    data = {"session_id": session_id, "seq": "5", "captured_ms": "1"}
    bad = await client.put(
        f"{API}/workspaces/{wid}/study/notes/{uuid.uuid4()}",
        headers=user_token_headers,
        data=data,
        files={"image": ("x.png", b"not a png", "image/png")},
    )
    assert bad.status_code == 422


async def test_failed_note_can_be_retried_once_per_click(
    client: AsyncClient,
    user_token_headers: dict,
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    notebook = await _notebook(client, wid, user_token_headers, title="T")
    session_id = await _start(client, wid, user_token_headers, notebook["id"])
    note_id, _ = await _put_note(client, wid, user_token_headers, session_id, 0)
    note = await db_session.get(StudyNote, uuid.UUID(note_id))
    note.status, note.error = "failed", "RuntimeError: stt down"
    await db_session.commit()
    url = f"{API}/workspaces/{wid}/study/notes/{note_id}/retry"
    retried = await client.post(url, headers=user_token_headers)
    assert retried.json()["status"] == "pending" and retried.json()["error"] is None
    await client.post(url, headers=user_token_headers)  # already pending: no-op
    keys = (await db_session.execute(select(TaskOutbox.dedupe_key))).scalars().all()
    assert sorted(keys) == [
        f"study-note:{note_id}:send:1",
        f"study-note:{note_id}:send:2",
    ]


async def test_notebooks_are_private_to_their_owner(
    client: AsyncClient,
    user_token_headers: dict,
    power_user_token_headers: dict,
    admin_token_headers: dict,
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    other_wid = await _make_workspace(client, user_token_headers, name="other")
    notebook = await _notebook(client, wid, user_token_headers, title="Mine")
    session_id = await _start(client, wid, user_token_headers, notebook["id"])
    note_id, _ = await _put_note(client, wid, user_token_headers, session_id, 0)
    base = f"{API}/workspaces/{wid}/study"
    # Someone outside the workspace.
    for path in (
        f"/notebooks/{notebook['id']}",
        f"/notebooks/{notebook['id']}/export",
        f"/notes/{note_id}/image",
    ):
        resp = await client.get(base + path, headers=power_user_token_headers)
        assert resp.status_code in (403, 404), path
        # An admin can open the workspace, but the notebook is not theirs.
        resp = await client.get(base + path, headers=admin_token_headers)
        assert resp.status_code == 404, path
    # The owner, through another workspace.
    wrong = await client.get(
        f"{API}/workspaces/{other_wid}/study/notebooks/{notebook['id']}",
        headers=user_token_headers,
    )
    assert wrong.status_code == 404
    _, stolen = await _put_note(client, wid, admin_token_headers, session_id, 3)
    assert stolen.status_code == 404
    listed = await client.get(f"{base}/notebooks", headers=admin_token_headers)
    assert listed.json() == []


async def test_recording_rules_reach_its_notes(
    client: AsyncClient,
    user_token_headers: dict,
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
    normal_user: User,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    notebook = await _notebook(client, wid, user_token_headers, title="T")
    session_id = await _start(client, wid, user_token_headers, notebook["id"])
    note_id, _ = await _put_note(client, wid, user_token_headers, session_id, 0)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    rec_id = rec.id
    base = f"{API}/workspaces/{wid}"
    linked = await client.patch(
        f"{base}/study/sessions/{session_id}",
        headers=user_token_headers,
        json={"recording_id": str(rec_id)},
    )
    assert linked.json()["recording_id"] == str(rec_id)
    by_recording = await client.get(
        f"{base}/study/notebooks?recording_id={rec_id}", headers=user_token_headers
    )
    assert [n["id"] for n in by_recording.json()] == [notebook["id"]]

    # A restricted recording: exporting its entries needs an explicit grant.
    resp = await client.patch(
        f"{base}/recordings/{rec_id}/sensitivity",
        headers=user_token_headers,
        json={"sensitivity": "restricted"},
    )
    assert resp.status_code == 200, resp.text
    export_url = f"{base}/study/notebooks/{notebook['id']}/export"
    denied = await client.get(export_url, headers=user_token_headers)
    assert denied.status_code == 403
    await client.patch(
        f"{base}/recordings/{rec_id}/sensitivity",
        headers=user_token_headers,
        json={"sensitivity": "internal"},
    )
    assert (await client.get(export_url, headers=user_token_headers)).status_code == 200

    # Deleting the recording removes its notes and asks for their media to go.
    deleted = await client.delete(
        f"{base}/recordings/{rec_id}", headers=user_token_headers
    )
    assert deleted.status_code == 204, deleted.text
    db_session.expire_all()
    assert await db_session.get(StudyNote, uuid.UUID(note_id)) is None
    objects = (
        (
            await db_session.execute(
                select(AudioObject).where(AudioObject.kind.like("study_%"))
            )
        )
        .scalars()
        .all()
    )
    assert len(objects) == 2
    assert {o.status for o in objects} == {"delete_requested"}
    assert {o.delete_reason for o in objects} == {"recording_delete"}
    detail = await client.get(
        f"{base}/study/notebooks/{notebook['id']}", headers=user_token_headers
    )
    assert detail.json()["notes"] == []


async def test_cancelled_session_discards_its_notes(
    client: AsyncClient,
    user_token_headers: dict,
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    notebook = await _notebook(client, wid, user_token_headers, title="T")
    session_id = await _start(client, wid, user_token_headers, notebook["id"])
    await _put_note(client, wid, user_token_headers, session_id, 0)
    url = f"{API}/workspaces/{wid}/study/sessions/{session_id}"
    assert (await client.delete(url, headers=user_token_headers)).status_code == 204
    assert (await client.delete(url, headers=user_token_headers)).status_code == 204
    assert (await db_session.execute(select(StudyNote))).scalars().all() == []


async def test_tags_and_a_thought_come_with_the_capture_and_can_change(
    client: AsyncClient,
    user_token_headers: dict,
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    wid = await _make_workspace(client, user_token_headers)
    notebook = await _notebook(client, wid, user_token_headers, title="T")
    base = f"{API}/workspaces/{wid}/study"
    found = await client.get(f"{base}/features", headers=user_token_headers)
    assert found.json() == {"enrich": False, "web_research": False}
    session_id = await _start(client, wid, user_token_headers, notebook["id"])
    note_id, resp = await _put_note(
        client,
        wid,
        user_token_headers,
        session_id,
        0,
        audio=False,
        tags="unclear,nonsense,important",
        comment=" Why not a full scan? ",
    )
    body = resp.json()
    # Known marks only, in a fixed order; the thought is the learner's comment.
    assert body["tags"] == ["important", "unclear"]
    assert body["comment"] == "Why not a full scan?"
    assert body["enrich_status"] == "none"  # no model configured
    url = f"{base}/notes/{note_id}"
    changed = await client.patch(url, headers=user_token_headers, json={"tags": []})
    assert changed.json()["tags"] == [] and changed.json()["comment"]
    bad = await client.patch(url, headers=user_token_headers, json={"tags": ["exam"]})
    assert bad.status_code == 422
    refused = await client.post(f"{url}/enrich", headers=user_token_headers)
    assert refused.status_code == 409

    # With a model: a screenshot is read at once, and marking a note
    # "did not understand" later asks for the explanation.
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://llm/v1")
    second, resp = await _put_note(
        client, wid, user_token_headers, session_id, 1, audio=False
    )
    assert resp.json()["enrich_status"] == "pending"
    note = await db_session.get(StudyNote, uuid.UUID(second))
    note.enrich_status = "ready"
    await db_session.commit()
    marked = await client.patch(
        f"{base}/notes/{second}", headers=user_token_headers, json={"tags": ["unclear"]}
    )
    assert marked.json()["enrich_status"] == "pending"
    tasks = (await db_session.execute(select(TaskOutbox))).scalars().all()
    assert [row.task_name for row in tasks] == ["tasks.enrich_study_note"] * 2
    # A session where the learner switched the model work off stays untouched.
    quiet = await _start(client, wid, user_token_headers, notebook["id"], enrich=False)
    _, resp = await _put_note(client, wid, user_token_headers, quiet, 0, audio=False)
    assert resp.json()["enrich_status"] == "none"


async def test_wider_context_comes_from_the_recordings_transcript(
    client: AsyncClient,
    user_token_headers: dict,
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
    normal_user: User,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    notebook = await _notebook(client, wid, user_token_headers, title="T")
    session_id = await _start(client, wid, user_token_headers, notebook["id"])
    note_id, _ = await _put_note(client, wid, user_token_headers, session_id, 0)
    url = f"{API}/workspaces/{wid}/study/notes/{note_id}/context"
    # The recording is not uploaded yet: there is nothing to read from.
    early = await client.get(url, headers=user_token_headers)
    assert early.json()["available"] is False
    assert early.json()["reason"] == "not_linked"

    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    await client.patch(
        f"{API}/workspaces/{wid}/study/sessions/{session_id}",
        headers=user_token_headers,
        json={"recording_id": str(rec.id)},
    )
    # The note's audio is 08:27–09:03; the seeded talk is in the first 2 seconds.
    near = (await client.get(url, headers=user_token_headers)).json()
    assert near["available"] is True and near["segments"] == []
    assert (near["start_ms"], near["end_ms"]) == (477_000, 573_000)
    assert near["at_start"] is False and near["at_end"] is True
    wide = (await client.get(f"{url}?seconds=600", headers=user_token_headers)).json()
    assert [s["text"] for s in wide["segments"]] == ["Hello everyone.", "Hi there."]
    assert wide["start_ms"] == 0 and wide["at_start"] is True
    assert wide["segments"][0]["speaker"]


async def test_a_marked_question_is_queued_for_an_answer(
    client: AsyncClient,
    user_token_headers: dict,
    meetings_enabled: _FakeAudioStore,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://llm/v1")
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)
    wid = await _make_workspace(client, user_token_headers)
    notebook = await _notebook(client, wid, user_token_headers, title="T")
    base = f"{API}/workspaces/{wid}/study"
    found = await client.get(f"{base}/features", headers=user_token_headers)
    assert found.json() == {"enrich": True, "web_research": True}
    session_id = await _start(
        client, wid, user_token_headers, notebook["id"], research_web=True
    )
    # The answer waits for the transcript: nothing but transcription is queued.
    _, resp = await _put_note(
        client, wid, user_token_headers, session_id, 0, tags="question"
    )
    assert resp.json()["tags"] == ["question"]
    assert resp.json()["answer_status"] == "none"
    # A question without sound cannot be heard.
    _, silent = await _put_note(
        client, wid, user_token_headers, session_id, 1, audio=False, tags="question"
    )
    assert (silent.json()["answer_status"], silent.json()["answer_error"]) == (
        "failed",
        "no_speech",
    )
    # An ordinary note that is marked as a question later, or asked again.
    plain, _ = await _put_note(client, wid, user_token_headers, session_id, 2)
    note = await db_session.get(StudyNote, uuid.UUID(plain))
    note.status, note.text = "ready", "How do we back up the database?"
    await db_session.commit()
    marked = await client.patch(
        f"{base}/notes/{plain}", headers=user_token_headers, json={"tags": ["question"]}
    )
    assert marked.json()["answer_status"] == "pending"
    one = await client.get(f"{base}/notes/{plain}", headers=user_token_headers)
    assert one.json()["id"] == plain and one.json()["answer_status"] == "pending"
    note = await db_session.get(StudyNote, uuid.UUID(plain))
    await db_session.refresh(note)
    note.answer_status = "failed"
    await db_session.commit()
    again = await client.post(
        f"{base}/notes/{plain}/answer", headers=user_token_headers
    )
    assert again.json()["answer_status"] == "pending"
    names = [
        row.task_name
        for row in (await db_session.execute(select(TaskOutbox))).scalars().all()
    ]
    assert names.count("tasks.answer_study_question") == 2
