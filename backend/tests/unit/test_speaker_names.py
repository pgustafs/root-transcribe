"""Unit tests for finding speakers' names in a transcript, and the worker task."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.models.speaker_name_run import SpeakerNameRun
from app.services import speaker_names
from app.services.agent_runner import AgentAnswer, AgentRunError
from app.services.speaker_names import Turn, parse_suggestions
from app.worker import tasks as tasks_module

from .test_meeting_notes import _database_with_note

pytestmark = pytest.mark.unit

TURNS = [
    Turn("SPEAKER_00", None, 0, "Welcome everyone. Over to you, Anna."),
    Turn("SPEAKER_01", None, 5000, "Tack. Hej, jag heter Anna och jobbar med ekonomi."),
    Turn(
        "SPEAKER_02", None, 12000, "Hi, my name is Peter. I talked to Lisa yesterday."
    ),
    Turn("SPEAKER_03", "Erik Lund", 20000, "Erik here, thanks Peter."),
]


class FakeRunner:
    def __init__(self, answers: list[str], context: int | None = None) -> None:
        self.answers = answers
        self.context = context
        self.calls: list[dict[str, str]] = []

    def context_tokens_sync(self) -> int | None:
        return self.context

    def run_sync(self, *, instructions: str, text: str) -> AgentAnswer:
        self.calls.append({"instructions": instructions, "text": text})
        return AgentAnswer(text=self.answers[len(self.calls) - 1], model="m")


def test_suggestions_in_swedish_and_english_with_the_quote_located() -> None:
    answer = """Here is what I found:
```json
[
  {"speaker": "Speaker 2", "name": "Anna", "quote": "jag heter Anna",
   "confidence": "high"},
  {"speaker": "speaker 3", "name": "Peter", "quote": "Hi, my name is Peter.",
   "confidence": "high"}
]
```"""
    assert parse_suggestions(answer, TURNS) == [
        {
            "speaker_label": "SPEAKER_01",
            "name": "Anna",
            "quote": "jag heter Anna",
            "start_ms": 5000,
            "confidence": "high",
            "status": "pending",
        },
        {
            "speaker_label": "SPEAKER_02",
            "name": "Peter",
            "quote": "Hi, my name is Peter.",
            "start_ms": 12000,
            "confidence": "high",
            "status": "pending",
        },
    ]


@pytest.mark.parametrize(
    "item",
    [
        # The quote is not in the transcript: invented evidence.
        '{"speaker": "Speaker 1", "name": "Olof", "quote": "my name is Olof"}',
        # A speaker that does not exist, and one that already has a name.
        '{"speaker": "Speaker 9", "name": "Anna", "quote": "jag heter Anna"}',
        '{"speaker": "Speaker 4", "name": "Erik", "quote": "Erik here"}',
        # The quote exists but does not contain the name.
        '{"speaker": "Speaker 1", "name": "Olof", "quote": "Welcome everyone"}',
        # Not a name.
        '{"speaker": "Speaker 1", "name": "Speaker 1", "quote": "Speaker 1"}',
        '{"speaker": "Speaker 1", "name": "", "quote": "Welcome everyone"}',
        '"just a string"',
    ],
)
def test_unusable_suggestions_are_dropped(item: str) -> None:
    assert parse_suggestions(f"[{item}]", TURNS) == []


@pytest.mark.parametrize("answer", ["", "No names found.", "[", "{}", "[1, 2"])
def test_answers_without_a_list_give_nothing(answer: str) -> None:
    assert parse_suggestions(answer, TURNS) == []


def test_one_suggestion_per_speaker_and_addressed_names() -> None:
    answer = """[
      {"speaker": "Speaker 2", "name": "Anna", "quote": "Over to you, Anna",
       "confidence": "medium"},
      {"speaker": "Speaker 2", "name": "Anna", "quote": "jag heter anna",
       "confidence": "high"},
      {"speaker": "Speaker 2", "name": "Lisa", "quote": "I talked to Lisa",
       "confidence": "whatever"}
    ]"""
    (only,) = parse_suggestions(answer, TURNS)
    # The stronger evidence wins; an unknown confidence counts as medium.
    assert (only["name"], only["confidence"], only["start_ms"]) == (
        "Anna",
        "high",
        5000,
    )
    # A quote spoken by someone else points at that person's turn.
    (addressed,) = parse_suggestions(
        '[{"speaker": "Speaker 2", "name": "Anna", "quote": "over to you anna"}]', TURNS
    )
    assert (addressed["start_ms"], addressed["confidence"]) == (0, "medium")


def test_the_model_sees_friendly_labels_and_existing_names() -> None:
    runner = FakeRunner(["[]"])
    suggestions, model = speaker_names.suggest_names(runner, TURNS, max_chars=10_000)
    assert (suggestions, model) == ([], "m")
    text = runner.calls[0]["text"]
    assert "[0:00:00] Speaker 1: Welcome everyone." in text
    assert "[0:00:20] Erik Lund: Erik here" in text
    assert "SPEAKER_" not in text
    assert runner.calls[0]["instructions"] == speaker_names.INSTRUCTIONS


def test_a_long_meeting_is_read_in_parts_and_merged() -> None:
    runner = FakeRunner(
        [
            '[{"speaker": "Speaker 2", "name": "Anna", "quote": "Over to you, Anna"}]',
            '[{"speaker": "Speaker 2", "name": "Anna", "quote": "jag heter Anna", '
            '"confidence": "high"}]',
            '[{"speaker": "Speaker 3", "name": "Peter", "quote": "my name is Peter", '
            '"confidence": "high"}]',
            "[]",
        ]
    )
    suggestions, _ = speaker_names.suggest_names(runner, TURNS, max_chars=70)
    assert len(runner.calls) == 4
    assert [(s["speaker_label"], s["name"], s["confidence"]) for s in suggestions] == [
        ("SPEAKER_01", "Anna", "high"),
        ("SPEAKER_02", "Peter", "high"),
    ]


def test_no_model_run_when_everyone_has_a_name() -> None:
    runner = FakeRunner([])
    named = [Turn("SPEAKER_00", "Anna", 0, "Hello.")]
    assert speaker_names.suggest_names(runner, named, max_chars=1000) == ([], None)
    assert runner.calls == []


# --- the worker task ---------------------------------------------------------


def _run(monkeypatch: pytest.MonkeyPatch, session, run, runner):
    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr("app.services.agent_runner.get_agent_runner", lambda: runner)
    return tasks_module.identify_speaker_names(str(run.id))


def _database_with_run():
    # Anna Berg (directory) says "Hello everyone."; SPEAKER_01 says "Hi there."
    session, note = _database_with_note()
    run = SpeakerNameRun(recording_id=note.recording_id, revision_id=note.revision_id)
    session.add(run)
    session.commit()
    return session, run


def test_task_stores_checked_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    session, run = _database_with_run()
    runner = FakeRunner(
        [
            '[{"speaker": "Speaker 2", "name": "Hi", "quote": "Hi there.", '
            '"confidence": "high"}, '
            '{"speaker": "Speaker 2", "name": "Bo", "quote": "I am Bo"}]'
        ]
    )
    assert _run(monkeypatch, session, run, runner) == {
        "status": "ready",
        "suggestions": "1",
    }
    session.refresh(run)
    assert run.status == "ready" and run.model == "m" and run.duration_ms is not None
    assert [(s["speaker_label"], s["name"]) for s in run.suggestions] == [
        ("SPEAKER_01", "Hi")
    ]
    # The directory person is shown by name; only the other speaker is asked about.
    assert "Anna Berg: Hello everyone." in runner.calls[0]["text"]
    assert "Speaker 2: Hi there." in runner.calls[0]["text"]
    # A duplicate delivery does nothing.
    assert _run(monkeypatch, session, run, runner)["status"] == "skipped"
    session.close()


def test_task_failure_is_recorded_and_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    session, run = _database_with_run()

    class Down(FakeRunner):
        def run_sync(self, *, instructions: str, text: str) -> AgentAnswer:
            raise AgentRunError("The agent service could not be reached.")

    with pytest.raises(AgentRunError):
        _run(monkeypatch, session, run, Down([]))
    session.refresh(run)
    assert (run.status, run.error) == (
        "failed",
        "The agent service could not be reached.",
    )
    session.close()


def test_task_refuses_a_too_small_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = _database_with_run()
    runner = FakeRunner([], context=8192)
    with pytest.raises(AgentRunError, match="8,192 tokens"):
        _run(monkeypatch, session, run, runner)
    assert runner.calls == []
    session.close()
