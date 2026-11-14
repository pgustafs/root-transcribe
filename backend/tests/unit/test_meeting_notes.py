"""Unit tests for note writing (prompts, long meetings) and the worker task."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.services import meeting_notes, note_export
from app.services.agent_runner import AgentAnswer, AgentRunError
from app.services.note_defaults import OUTPUT_RULES
from app.services.transcript_export import ExportSegment
from app.worker import tasks as tasks_module

pytestmark = pytest.mark.unit


class FakeRunner:
    """Records every run; answers with a numbered document."""

    def __init__(
        self, fail: Exception | None = None, context: int | None = None
    ) -> None:
        self.calls: list[dict[str, str]] = []
        self.fail = fail
        self.context = context

    def context_tokens_sync(self) -> int | None:
        return self.context

    def run_sync(self, *, instructions: str, text: str) -> AgentAnswer:
        self.calls.append({"instructions": instructions, "text": text})
        if self.fail:
            raise self.fail
        return AgentAnswer(
            text=f"```markdown\n## Doc {len(self.calls)}\n```", model="m"
        )


def test_transcript_lines_and_split_parts() -> None:
    lines = meeting_notes.transcript_lines(
        [
            ExportSegment(0, 1000, "Anna", " Hello. "),
            ExportSegment(3_661_000, 3_662_000, "SPEAKER_01", "Hi."),
        ]
    )
    assert lines == ["[0:00:00] Anna: Hello.", "[1:01:01] SPEAKER_01: Hi."]
    parts = meeting_notes.split_parts(["a" * 40, "b" * 40, "c" * 40], max_chars=90)
    assert parts == ["a" * 40 + "\n" + "b" * 40, "c" * 40]
    # Whole lines stay together; an over-long line is cut, never dropped.
    assert meeting_notes.split_parts(["x" * 500], max_chars=100) == ["x" * 100]
    assert meeting_notes.split_parts([], max_chars=100) == []


def test_clean_markdown_unwraps_a_fenced_document() -> None:
    assert (
        meeting_notes.clean_markdown("```markdown\n# T\n\ntext\n```") == "# T\n\ntext"
    )
    assert meeting_notes.clean_markdown("```\n# T\n```\n") == "# T"
    inner = "# T\n\n```\ncode\n```\n\nmore"  # a fence INSIDE the document stays
    assert meeting_notes.clean_markdown(inner) == inner


def test_short_meeting_is_one_run() -> None:
    runner, steps = FakeRunner(), []
    answer = meeting_notes.write_note(
        runner,
        instructions="Summarize.",
        title="Weekly",
        lines=["[0:00:00] Anna: Hello."],
        max_chars=1000,
        on_step=steps.append,
    )
    assert answer == AgentAnswer(text="## Doc 1", model="m")
    assert steps == ["write"]
    (call,) = runner.calls
    assert call["instructions"] == f"Summarize.\n\n{OUTPUT_RULES}"
    assert (
        "Meeting: Weekly" in call["text"] and "[0:00:00] Anna: Hello." in call["text"]
    )


def test_long_meeting_is_written_in_parts_then_merged() -> None:
    runner, steps = FakeRunner(), []
    lines = [f"[0:00:0{i}] Anna: " + "word " * 10 for i in range(6)]
    answer = meeting_notes.write_note(
        runner,
        instructions="Summarize.",
        title="Long",
        lines=lines,
        max_chars=150,
        on_step=steps.append,
    )
    assert steps == ["part:1/3", "part:2/3", "part:3/3", "merge"]
    assert len(runner.calls) == 4
    assert "part 2 of 3" in runner.calls[1]["text"]
    merge = runner.calls[3]["text"]
    assert "--- Part 1 of 3 ---\n## Doc 1" in merge and "## Doc 3" in merge
    assert "Anna:" not in merge  # the merge sees the partial notes, not the transcript
    assert answer.text == "## Doc 4"


def _database_with_note(text: str = "Hello everyone."):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.meeting_note import MeetingNote
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_transcript_revision import MeetingTranscriptRevision
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.models.speaker_profile import SpeakerProfile
    from app.models.workspace import Workspace

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    ws = Workspace(name="ws")
    session.add(ws)
    session.flush()
    rec = MeetingRecording(workspace_id=ws.id, title="Weekly", status="completed")
    anna = SpeakerProfile(display_name="Anna Berg")
    session.add_all([rec, anna])
    session.flush()
    revision = MeetingTranscriptRevision(
        recording_id=rec.id, revision_number=1, state="approved"
    )
    session.add(revision)
    session.flush()
    session.add_all(
        [
            MeetingTranscriptSegment(
                recording_id=rec.id,
                revision_id=revision.id,
                idx=0,
                start_ms=0,
                end_ms=1000,
                speaker_label="SPEAKER_00",
                speaker_profile_id=anna.id,
                text=text,
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
    note = MeetingNote(
        workspace_id=ws.id,
        recording_id=rec.id,
        revision_id=revision.id,
        agent_name="Summary",
        instructions="Summarize.",
        status="pending",
    )
    session.add(note)
    session.commit()
    return session, note


def _run_task(monkeypatch: pytest.MonkeyPatch, session, note, runner):
    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr("app.services.agent_runner.get_agent_runner", lambda: runner)
    return tasks_module.generate_meeting_note(str(note.id))


def test_task_writes_the_note_with_speaker_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, note = _database_with_note()
    runner = FakeRunner()
    assert _run_task(monkeypatch, session, note, runner)["status"] == "ready"
    session.refresh(note)
    assert note.status == "ready" and note.current_step is None
    assert note.generated_markdown == note.content_markdown == "## Doc 1"
    assert note.model == "m" and note.duration_ms is not None
    text = runner.calls[0]["text"]
    # Enrolled people by name, unknown speakers by label, in order.
    assert (
        "[0:00:00] Anna Berg: Hello everyone.\n[0:00:01] SPEAKER_01: Hi there." in text
    )
    # A duplicate delivery does nothing.
    assert _run_task(monkeypatch, session, note, runner)["status"] == "skipped"
    assert len(runner.calls) == 1
    session.close()


def test_task_records_a_readable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session, note = _database_with_note()
    runner = FakeRunner(fail=AgentRunError("The agent service could not be reached."))
    # Raised, so the job list shows a failed job (not "Success").
    with pytest.raises(AgentRunError):
        _run_task(monkeypatch, session, note, runner)
    session.refresh(note)
    assert note.status == "failed"
    assert note.error == "The agent service could not be reached."
    assert note.content_markdown is None
    session.close()


def test_task_refuses_a_model_with_too_small_a_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, note = _database_with_note()
    runner = FakeRunner(context=8192)
    with pytest.raises(AgentRunError, match="8,192 tokens"):
        _run_task(monkeypatch, session, note, runner)
    session.refresh(note)
    assert note.status == "failed" and "at least 16,000" in note.error
    assert runner.calls == []  # no model time spent on a run that cannot work
    session.close()


def test_parts_are_sized_to_the_served_context_window() -> None:
    budget = meeting_notes.input_budget_chars
    assert budget(None, "Summarize.", 48000) == 48000  # unknown: the setting
    assert budget(131072, "Summarize.", 48000) == 48000  # roomy: the setting
    small = budget(16000, "Summarize.", 48000)
    assert 20000 < small < 27000  # (16000 - 5000 - 2000 - instructions) * 3
    assert budget(16000, "x" * 9000, 48000) < small  # long instructions take room
    with pytest.raises(meeting_notes.ContextTooSmall):
        budget(8192, "Summarize.", 48000)


def test_task_reraises_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    session, note = _database_with_note()
    with pytest.raises(ValueError):
        _run_task(monkeypatch, session, note, FakeRunner(fail=ValueError("boom")))
    session.refresh(note)
    assert (note.status, note.error) == ("failed", "ValueError: boom")
    session.close()


def test_agent_runner_client_maps_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.services import agent_runner

    seen: dict = {}
    replies = [
        {"answer": " # Hi ", "error": None, "model": "qwen"},
        {"answer": "", "error": "model timed out"},
        {"answer": "  ", "error": None},
    ]

    def post(url, json, timeout):
        seen.update(url=url, json=json)
        return httpx.Response(
            200, json=replies.pop(0), request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(agent_runner.httpx, "post", post)
    answer = agent_runner.AgentRunner().run_sync(instructions="I", text="T")
    assert answer == AgentAnswer(text="# Hi", model="qwen")
    assert seen["url"] == "http://localhost:8011/run"
    assert (seen["json"]["instructions"], seen["json"]["input"]) == ("I", "T")
    for message in ("model timed out", "empty answer"):
        with pytest.raises(AgentRunError, match=message):
            agent_runner.AgentRunner().run_sync(instructions="I", text="T")

    def refuse(url, json, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(agent_runner.httpx, "post", refuse)
    with pytest.raises(AgentRunError, match="could not be reached"):
        agent_runner.AgentRunner().run_sync(instructions="I", text="T")

    def health(url, timeout):
        assert url == "http://localhost:8011/health"
        return httpx.Response(
            200, json={"context_tokens": 32768}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(agent_runner.httpx, "get", health)
    assert agent_runner.AgentRunner().context_tokens_sync() == 32768

    def down(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(agent_runner.httpx, "get", down)
    assert agent_runner.AgentRunner().context_tokens_sync() is None


def test_export_parses_the_markdown_people_write() -> None:
    blocks = note_export.parse(
        "# Title\n\nText with **bold** and `code`.\n\n"
        "- [ ] open\n- [x] done\n  - nested\n\n1. one\n2. two\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n> quote\n\n```\ncode\n```\n"
    )
    kinds = [(b.kind, b.level, b.number, b.checked) for b in blocks]
    assert kinds == [
        ("heading", 1, None, None),
        ("paragraph", 0, None, None),
        ("item", 0, None, False),
        ("item", 0, None, True),
        ("item", 1, None, None),
        ("item", 0, 1, None),
        ("item", 0, 2, None),
        ("table", 0, None, None),
        ("quote", 0, None, None),
        ("code", 0, None, None),
    ]
    bold = [run.text for run in blocks[1].runs if run.bold]
    assert bold == ["bold"] and blocks[7].rows[1][1][0].text == "2"
    assert note_export.render("md", title="T", markdown="# A").decode() == "# A\n"
    assert note_export.render("pdf", title="T", markdown="").startswith(b"%PDF-")
    with pytest.raises(ValueError):
        note_export.render("odt", title="T", markdown="x")
