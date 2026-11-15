"""Unit tests for study notes: source identity, export, delivery, the task."""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app.crud import study as study_crud
from app.services import study_commands, study_export, study_source
from app.services.study_export import ExportNote
from app.worker import tasks as tasks_module

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=512s&list=PL123&si=abc",
        "https://youtu.be/dQw4w9WgXcQ?t=90",
        "youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ?start=3",
        "https://www.youtube.com/live/dQw4w9WgXcQ?utm_source=x",
    ],
)
def test_every_youtube_link_shape_is_one_source(url: str) -> None:
    assert study_source.source_key(url) == "youtube:dQw4w9WgXcQ"


def test_other_links_have_no_identity_of_their_own() -> None:
    assert study_source.source_key("https://example.com/course/3") is None
    assert study_source.source_key("https://www.youtube.com/watch?v=short") is None
    assert study_source.source_key("https://www.youtube.com/@channel") is None
    assert study_source.source_key("  ") is None
    assert study_source.is_http_url("example.com/a")
    assert not study_source.is_http_url("javascript:alert(1)")
    assert study_source.normalized("example.com/a") == "https://example.com/a"


def _note(session: str, started: datetime, seq: int, **kw) -> ExportNote:
    values = {
        "captured_ms": 522_000,
        "audio_start_ms": 507_000,
        "audio_end_ms": 543_000,
        "status": "ready",
        "text": "The database can use this index.",
        "image": b"png-" + f"{session}{seq}".encode(),
    }
    values.update(kw)
    return ExportNote(session_id=session, session_started=started, seq=seq, **values)


def test_export_is_in_capture_order_with_truthful_states() -> None:
    first = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    second = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    notes = [
        _note("b", second, 0, status="failed", text=None),
        _note("a", first, 1, status="pending", text=None, captured_ms=3_725_000),
        _note("a", first, 0),
        _note(
            "b",
            second,
            1,
            status="screenshot_only",
            text=None,
            audio_start_ms=None,
            audio_end_ms=None,
        ),
        _note("b", second, 2, status="no_speech", text=None),
    ]
    markdown, images = study_export.build_markdown(
        title="Query plans",
        source_url="https://www.youtube.com/watch?v=VIDEO_ID",
        notes=notes,
    )
    assert markdown.startswith(
        "# Query plans\n\nSource: https://www.youtube.com/watch?v=VIDEO_ID\n\n"
        "## Study session · 15 November 2026\n\n### 08:42 in this recording\n\n"
        "![Query plans, captured at 08:42](images/note-001.png)\n\n"
        "**Audio 08:27–09:03**\n\n> The database can use this index.\n"
    )
    assert "### 1:02:05 in this recording" in markdown
    assert markdown.count("## Study session") == 2
    assert markdown.index("15 November") < markdown.index("16 November")
    for text in (
        "*Transcription pending.*",
        "*Transcription failed.*",
        "*No speech detected.*",
        "*Screenshot only. Tab audio was unavailable.*",
    ):
        assert text in markdown
    # Stable, collision-free names in capture order; every image is kept.
    assert [path for path, _ in images] == [
        f"images/note-00{n}.png" for n in range(1, 6)
    ]
    assert images[0][1] == b"png-a0" and images[2][1] == b"png-b0"


def test_zip_opens_offline_with_relative_images() -> None:
    body = study_export.build_zip(
        title="Frågor & svar: del 1",
        source_url=None,
        notes=[_note("a", datetime(2026, 1, 2, tzinfo=UTC), 0)],
    )
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()
        assert names == ["Frågor-svar-del-1.md", "images/note-001.png"]
        document = archive.read(names[0]).decode("utf-8")
        assert "Source:" not in document
        assert "(images/note-001.png)" in document
        assert archive.read("images/note-001.png") == b"png-a0"


def test_shortcut_secret_is_stored_as_a_hash_only() -> None:
    token = study_crud.new_token()
    assert token.startswith("rtk_") and len(token) > 40
    assert study_crud.new_token() != token
    digest = study_crud.hash_token(token)
    assert len(digest) == 64 and token not in digest


async def test_a_press_reaches_the_recorder_and_gets_its_answer() -> None:
    bus = study_commands.MemoryCommandBus()
    user = uuid.uuid4()
    with pytest.raises(study_commands.NotConnected):
        await bus.deliver(user, {"invocation": "x"}, timeout=0.1)

    received: list[dict] = []

    async def recorder() -> None:
        async for command in bus.listen(user, "conn", "session-1"):
            if command is None:
                continue
            received.append(command)
            await bus.acknowledge(user, command["invocation"], {"outcome": "started"})

    task = asyncio.create_task(recorder())
    await asyncio.sleep(0)
    assert await bus.recorders(user) == {"conn": "session-1"}
    assert await bus.recorders(uuid.uuid4()) == {}
    answer = await bus.deliver(user, {"invocation": "p1"}, timeout=1)
    assert answer == {"outcome": "started"} and len(received) == 1
    # The stored answer serves a repeated press without a second delivery.
    assert await bus.result(user, "p1") == {"outcome": "started"}
    assert await bus.result(user, "other") is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await bus.recorders(user) == {}


async def test_a_recorder_that_does_not_confirm_times_out() -> None:
    bus = study_commands.MemoryCommandBus()
    user = uuid.uuid4()

    async def silent() -> None:
        async for _ in bus.listen(user, "conn", "s"):
            pass

    task = asyncio.create_task(silent())
    await asyncio.sleep(0)
    assert await bus.deliver(user, {"invocation": "p"}, timeout=0.05) is None
    task.cancel()


# --- the worker task ----------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.blobs = {"a.wav": b"RIFFxxxxWAVE"}

    def get_sync(self, key: str) -> bytes:
        return self.blobs[key]


class _Stt:
    def __init__(self, text: str = " Hello there. ", fail: bool = False) -> None:
        self.text, self.fail, self.calls = text, fail, []

    def transcribe_sync(self, audio, filename, *, language=None):
        self.calls.append((audio, filename, language))
        if self.fail:
            raise RuntimeError("stt down")
        return type("R", (), {"text": self.text})()


def _database(language: str = "sv"):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.study import StudyNote, StudyNotebook, StudySession
    from app.models.user import User
    from app.models.workspace import Workspace

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    ws = Workspace(name="ws")
    user = User(email="a@example.com", hashed_password="")
    session.add_all([ws, user])
    session.flush()
    notebook = StudyNotebook(workspace_id=ws.id, owner_id=user.id, title="T")
    session.add(notebook)
    session.flush()
    study_session = StudySession(
        id=uuid.uuid4(), notebook_id=notebook.id, language=language
    )
    session.add(study_session)
    session.flush()
    note = StudyNote(
        id=uuid.uuid4(),
        notebook_id=notebook.id,
        session_id=study_session.id,
        seq=0,
        captured_ms=1000,
        audio_key="a.wav",
        has_audio=True,
        status="pending",
    )
    session.add_all(
        [note, AudioObject(bucket="b", key="a.wav", kind="study_audio", ref_id=note.id)]
    )
    session.commit()
    return session, note


def _run(monkeypatch: pytest.MonkeyPatch, session, note, stt):
    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: _Store())
    monkeypatch.setattr(
        "app.services.transcription.get_transcription_service", lambda: stt
    )
    return tasks_module.transcribe_study_note(str(note.id))


def test_task_transcribes_and_asks_for_the_audio_to_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.audio_object import AudioObject

    session, note = _database()
    stt = _Stt()
    assert _run(monkeypatch, session, note, stt) == {"status": "ready"}
    session.refresh(note)
    assert (note.status, note.text) == ("ready", "Hello there.")
    assert stt.calls == [(b"RIFFxxxxWAVE", "note.wav", "sv")]
    obj = session.query(AudioObject).one()
    assert (obj.status, obj.delete_reason) == ("delete_requested", "transcribed")
    # The key is cleared only when the deletion is confirmed.
    obj.status = "deleted"
    tasks_module._finalize_confirmed_deletion(session, obj)
    assert note.audio_key is None
    # A duplicate delivery does nothing.
    assert _run(monkeypatch, session, note, stt)["status"] == "skipped"
    assert len(stt.calls) == 1
    session.close()


def test_task_reports_silence_and_auto_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, note = _database(language="auto")
    stt = _Stt(text="  ")
    assert _run(monkeypatch, session, note, stt) == {"status": "no_speech"}
    session.refresh(note)
    assert note.text is None and stt.calls[0][2] is None
    session.close()


def test_task_failure_keeps_the_audio_for_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.audio_object import AudioObject

    session, note = _database()
    with pytest.raises(RuntimeError):
        _run(monkeypatch, session, note, _Stt(fail=True))
    session.refresh(note)
    assert note.status == "failed" and "stt down" in note.error
    assert note.audio_key == "a.wav"
    assert session.query(AudioObject).one().status == "active"
    session.close()
