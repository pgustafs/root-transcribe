"""Unit tests for study notes: source identity, export, delivery, the task."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app.services import study_enrich, study_export, study_source
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
        _note("a", first, 0, comment="Compare with a full scan.\n\nAsk about it."),
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
        "## Recorded 15 November 2026\n\n### 08:42 in this recording\n\n"
        "![Query plans, captured at 08:42](images/note-001.png)\n\n"
        "**Audio 08:27–09:03**\n\n> The database can use this index.\n\n"
        "**My thoughts**\n\nCompare with a full scan.\n\nAsk about it.\n"
    )
    # Only the note that has the owner's own words gets the heading.
    assert markdown.count("**My thoughts**") == 1
    assert "### 1:02:05 in this recording" in markdown
    assert markdown.count("## Recorded ") == 2
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


# --- what the language model adds ----------------------------------------------

RAW = (
    "so um the the database can can use this index you know to find the "
    "matching rows and uh then it it reads only those rows from the table"
)
CLEAN = (
    "The database can use this index to find the matching rows. "
    "Then it reads only those rows from the table."
)


def test_tidy_text_must_stay_the_speakers_words() -> None:
    assert study_enrich.faithful(RAW, CLEAN)
    # A summary and an invention are both refused.
    assert not study_enrich.faithful(RAW, "The database uses an index.")
    assert not study_enrich.faithful(RAW, CLEAN + " " + CLEAN)
    assert not study_enrich.faithful("", CLEAN)


def test_model_answer_is_checked_field_by_field() -> None:
    body = {
        "title": "Index scan.",
        "clean_text": CLEAN,
        "terms": [
            {"term": "Index", "definition": "A lookup structure."},
            {"term": "index", "definition": "again"},
            {"term": "", "definition": "x"},
            "junk",
        ],
        "explanation": "It looks things up.",
    }
    answer = "```json\n" + json.dumps(body) + "\n```"
    parsed = study_enrich.parse_note(answer, text=RAW, unclear=False)
    assert parsed.title == "Index scan"
    assert parsed.clean_text == CLEAN
    assert parsed.terms == [{"term": "Index", "definition": "A lookup structure."}]
    # An explanation is kept only when the learner asked for one.
    assert parsed.explanation is None
    asked = study_enrich.parse_note(answer, text=RAW, unclear=True)
    assert asked.explanation == "It looks things up."
    # A "tidy" text that is really a summary is dropped; the rest is kept.
    short = study_enrich.parse_note(
        '{"title": "T", "clean_text": "Indexes.", "terms": []}', text=RAW, unclear=False
    )
    assert short.clean_text is None and short.title == "T"
    with pytest.raises(ValueError):
        study_enrich.parse_note("I cannot help with that.", text=RAW, unclear=False)


def test_slide_answer_none_means_nothing_to_read() -> None:
    assert study_enrich.parse_slide("NONE") is None
    assert study_enrich.parse_slide(" none. ") is None
    assert study_enrich.parse_slide("```markdown\n# Plan\n```") == "# Plan"
    text = study_enrich.build_input(text=RAW, slide_text="# Plan", unclear=True)
    assert "did not understand" in text and "# Plan" in text and RAW in text


class _Images:
    def get_sync(self, key: str) -> bytes:
        return b"png-bytes"


class _Runner:
    """Answers the two questions; ``vision`` False = a text-only model."""

    def __init__(self, *, vision: bool = True, note: str | None = None) -> None:
        self.vision, self.calls = vision, []
        self.note = note or json.dumps(
            {
                "title": "Index scan",
                "clean_text": CLEAN,
                "terms": [{"term": "Index", "definition": "A lookup structure."}],
                "explanation": "It looks things up.",
            }
        )

    def chat_sync(self, *, instructions, text="", image=None, **_):
        from app.services.agent_runner import AgentAnswer, AgentRunError

        self.calls.append(("image" if image else "text", text))
        if image is not None:
            if not self.vision:
                raise AgentRunError("this model does not support image input")
            return AgentAnswer(text="# Query plan\n\n```sql\nSELECT 1;\n```")
        return AgentAnswer(text=self.note)


def _enrich(monkeypatch: pytest.MonkeyPatch, session, note, runner):
    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: _Images())
    monkeypatch.setattr("app.services.agent_runner.get_agent_runner", lambda: runner)
    return tasks_module.enrich_study_note(str(note.id))


def _ready_note(tags=()):
    session, note = _database()
    note.status, note.text, note.image_key = "ready", RAW, "a.png"
    note.tags, note.enrich_status = list(tags), "pending"
    session.commit()
    return session, note


def test_enrich_adds_title_tidy_text_terms_and_slide_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, note = _ready_note()
    runner = _Runner()
    assert _enrich(monkeypatch, session, note, runner) == {"status": "ready"}
    session.refresh(note)
    assert note.title == "Index scan"
    assert note.clean_text == CLEAN
    # The transcript itself is never changed.
    assert note.text == RAW
    assert note.slide_text.startswith("# Query plan")
    assert note.terms == [{"term": "Index", "definition": "A lookup structure."}]
    assert note.explanation is None and note.enrich_error is None
    # The slide text is given to the second question.
    assert [kind for kind, _ in runner.calls] == ["image", "text"]
    assert "# Query plan" in runner.calls[1][1]
    # A duplicate delivery does nothing.
    assert _enrich(monkeypatch, session, note, runner)["status"] == "skipped"
    assert len(runner.calls) == 2
    session.close()


def test_enrich_explains_a_note_marked_unclear_and_survives_a_text_only_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, note = _ready_note(tags=["unclear"])
    runner = _Runner(vision=False)
    assert _enrich(monkeypatch, session, note, runner) == {"status": "ready"}
    session.refresh(note)
    assert note.explanation == "It looks things up."
    assert note.slide_text is None
    # The part that failed is named; the rest is in place.
    assert "does not support image input" in note.enrich_error
    assert note.title == "Index scan"
    session.close()


def test_enrich_that_gets_nothing_usable_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, note = _ready_note()
    runner = _Runner(vision=False, note="Sorry, no.")
    assert _enrich(monkeypatch, session, note, runner) == {"status": "failed"}
    session.refresh(note)
    assert note.enrich_status == "failed" and note.title is None
    assert note.text == RAW
    session.close()


def test_transcription_queues_the_model_work_when_a_model_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.task_outbox import TaskOutbox

    monkeypatch.setattr(tasks_module, "_nudge_outbox", lambda: None)
    session, note = _database()
    _run(monkeypatch, session, note, _Stt())
    session.refresh(note)
    # No model configured: nothing is queued.
    assert note.enrich_status == "none"
    assert session.query(TaskOutbox).count() == 0
    session.close()

    monkeypatch.setattr(tasks_module.settings, "LLM_BASE_URL", "http://llm/v1")
    session, note = _database()
    _run(monkeypatch, session, note, _Stt())
    session.refresh(note)
    assert note.enrich_status == "pending"
    row = session.query(TaskOutbox).one()
    assert row.task_name == "tasks.enrich_study_note"
    assert row.payload == {"note_id": str(note.id)}
    session.close()


def test_export_carries_what_was_added() -> None:
    note = _note(
        "a",
        datetime(2026, 11, 15, 10, 0, tzinfo=UTC),
        0,
        title="Index scan",
        tags=("important", "unclear"),
        slide_text="# Query plan",
        terms=(("Index", "A lookup structure."),),
        explanation="It looks things up.",
        comment="Ask about it.",
    )
    markdown, _ = study_export.build_markdown(
        title="Query plans", source_url=None, notes=[note]
    )
    assert "### Index scan\n\n08:42 in this recording\n\n" in markdown
    assert "*Important · Did not understand*" in markdown
    order = [
        markdown.index(part)
        for part in (
            "> The database",
            "**Explained simply**",
            "**Text on the slide**\n\n# Query plan",
            "**Key terms**\n\n- **Index**: A lookup structure.",
            "**My thoughts**",
            "## Glossary\n\n- **Index**: A lookup structure.",
        )
    ]
    assert order == sorted(order)
