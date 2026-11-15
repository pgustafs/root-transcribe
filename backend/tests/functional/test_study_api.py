"""Functional tests: study notebooks, notes, export and the keyboard shortcut."""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audio_object import AudioObject
from app.models.study import HotkeyCredential, StudyNote
from app.models.task_outbox import TaskOutbox
from app.models.user import User
from app.services import study_commands

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


@pytest.fixture
def bus():
    memory = study_commands.MemoryCommandBus()
    study_commands.set_command_bus(memory)
    yield memory
    study_commands.set_command_bus(None)


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


async def _put_note(client, wid, headers, session_id, seq, *, audio=True, note_id=None):
    note_id = note_id or str(uuid.uuid4())
    data = {"session_id": session_id, "seq": str(seq), "captured_ms": "522000"}
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


async def _credential(client, headers) -> str:
    resp = await client.post(f"{API}/study/hotkey/credential", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def test_shortcut_key_is_hashed_rotated_and_revoked(
    client: AsyncClient,
    user_token_headers: dict,
    db_session: AsyncSession,
    bus: study_commands.MemoryCommandBus,
) -> None:
    empty = await client.get(
        f"{API}/study/hotkey/credential", headers=user_token_headers
    )
    assert empty.json()["exists"] is False
    token = await _credential(client, user_token_headers)
    stored = (await db_session.execute(select(HotkeyCredential))).scalars().one()
    assert stored.token_hash != token and token.endswith(stored.hint)
    shown = await client.get(
        f"{API}/study/hotkey/credential", headers=user_token_headers
    )
    assert shown.json()["token"] is None and shown.json()["hint"] == token[-4:]

    def press(secret: str):
        return client.post(
            f"{API}/study/hotkey/test", headers={"Authorization": f"Bearer {secret}"}
        )

    ok = await press(token)
    assert ok.status_code == 200
    assert "Recorder: not running." in ok.text and "Connected as" in ok.text
    # The secret opens nothing but the shortcut routes.
    api = await client.get(
        f"{API}/workspaces/mine", headers={"Authorization": f"Bearer {token}"}
    )
    assert api.status_code == 401
    # Rotation: the old secret stops working at once.
    newer = await _credential(client, user_token_headers)
    assert (await press(token)).status_code == 401
    assert (await press(newer)).status_code == 200
    revoked = await client.delete(
        f"{API}/study/hotkey/credential", headers=user_token_headers
    )
    assert revoked.status_code == 204
    assert (await press(newer)).status_code == 401
    assert (await press("rtk_wrong")).status_code == 401
    assert (await client.post(f"{API}/study/hotkey/test")).status_code == 401


async def test_a_press_starts_and_stops_one_note(
    client: AsyncClient,
    user_token_headers: dict,
    normal_user: User,
    bus: study_commands.MemoryCommandBus,
) -> None:
    token = await _credential(client, user_token_headers)
    auth = {"Authorization": f"Bearer {token}"}

    idle = await client.post(f"{API}/study/hotkey", headers=auth)
    assert idle.status_code == 409 and "No recording" in idle.text

    seen: list[dict] = []
    answers = iter(
        [
            {"outcome": "started", "captured_ms": 522_000},
            {
                "outcome": "saved",
                "captured_ms": 522_000,
                "audio_start_ms": 507_000,
                "audio_end_ms": 543_000,
            },
            {"outcome": "error", "reason": "paused"},
        ]
    )

    async def browser(connection: str) -> None:
        async for command in bus.listen(normal_user.id, connection, "session"):
            if command is None:
                continue
            seen.append(command)
            # The browser reports through the API, like the real one.
            await client.post(
                f"{API}/study/commands/{command['invocation']}/ack",
                headers=user_token_headers,
                json=next(answers),
            )

    recorder = asyncio.create_task(browser("one"))
    await asyncio.sleep(0)

    def press(invocation: str):
        return client.post(
            f"{API}/study/hotkey", headers={**auth, "X-Invocation-Id": invocation}
        )

    started = await press("press-1")
    assert (started.status_code, started.text) == (200, "Note started · 08:42\n")
    assert started.headers["x-study-outcome"] == "started"
    # The same press again: the first answer, no second delivery.
    assert (await press("press-1")).text == "Note started · 08:42\n"
    assert len(seen) == 1 and seen[0]["action"] == "toggle"
    saved = await press("press-2")
    assert saved.text == "Note saved · 08:27–09:03\n"
    assert (await press("press-2")).text == saved.text and len(seen) == 2
    paused = await press("press-3")
    assert paused.status_code == 409 and "paused" in paused.text

    # A second open recorder makes the press ambiguous: nothing is captured.
    second = asyncio.create_task(browser("two"))
    await asyncio.sleep(0)
    ambiguous = await press("press-4")
    assert ambiguous.status_code == 409 and "Two recorders" in ambiguous.text
    assert len(seen) == 3
    for task in (recorder, second):
        task.cancel()
    await asyncio.sleep(0)
    gone = await press("press-5")
    assert gone.status_code == 409


async def test_a_press_that_is_not_confirmed_says_so(
    client: AsyncClient,
    user_token_headers: dict,
    normal_user: User,
    bus: study_commands.MemoryCommandBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(study_commands, "ACK_TIMEOUT_SECONDS", 0.05)
    token = await _credential(client, user_token_headers)

    async def silent() -> None:
        async for _ in bus.listen(normal_user.id, "one", "session"):
            pass

    task = asyncio.create_task(silent())
    await asyncio.sleep(0)
    resp = await client.post(
        f"{API}/study/hotkey", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 504 and "did not confirm" in resp.text
    task.cancel()
