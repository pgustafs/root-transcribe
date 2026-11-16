"""The cleaned transcript: what the model may change, and what it may not."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.transcript_cleanup import (
    CONTEXT_LINES,
    MAX_LINES,
    Line,
    accept,
    clean,
    parse_answer,
    parts,
    request_text,
)


def test_a_correction_is_taken_and_a_rewrite_is_not() -> None:
    # A misheard word, a name, a split word: corrections.
    assert accept(
        "Ska vi ha kvar Bok Demo och läsa om plattformen?",
        "Ska vi ha kvar Boka demo och läsa om plattformen?",
    )
    assert accept("Gärna, Thomas. Tja.", "Tjena, Thomas. Tja.")
    assert accept(
        "Vi sätter en rotel plattform i drift hos er.",
        "Vi sätter en rotad plattform i drift hos er.",
    )
    # Punctuation and capitals only: always fine, also on a short line.
    assert accept("ja exakt", "Ja, exakt.")
    assert accept("det var kenneth som delade den", "Det var Kenneth som delade den.")
    # Speech is not tidied up: dropping filler words or rephrasing is a rewrite.
    assert not accept(
        "Ja, men jag tror i början så är det ju bra ifall, liksom, vi gör så.",
        "Jag tror att det är bra om vi gör så i början.",
    )
    assert not accept(
        "Det är ju så jäkla skönt ändå numera tycker jag.", "Det är skönt numera."
    )
    # Nothing is invented at the end of a line that broke off.
    assert not accept(
        "Men annars, imorgon kommer jag",
        "Men annars, imorgon kommer jag in till kontoret som vanligt.",
    )
    # One or two words give no context: the words stay ("Vad" is not "Ja").
    assert not accept("Vad", "Ja.")
    assert not accept("Jobba lite.", "Jobbar lite.")
    # No change, or an empty answer, is not a correction.
    assert not accept("Ja, exakt.", "Ja, exakt.")
    assert not accept("Ja, exakt.", "  ")


def test_only_this_parts_lines_are_read_from_the_answer() -> None:
    answer = (
        "Here are the corrected lines:\n"
        "12| Tjena, Thomas.\n"
        "`13| Det var skitkul i fredags.`\n"
        "13| a second answer for the same line is ignored\n"
        "40| a line of another part\n"
        "7| a context line\n"
        "not a line at all\n"
        "14|\n"
    )
    assert parse_answer(answer, 10, 20) == {
        11: "Tjena, Thomas.",
        12: "Det var skitkul i fredags.",
    }
    assert parse_answer("NONE", 0, 40) == {}


def test_long_transcripts_go_in_parts_with_context() -> None:
    lines = [Line("Speaker 1", f"line {n}") for n in range(MAX_LINES * 2 + 5)]
    ranges = parts(lines)
    assert ranges == [
        (0, MAX_LINES),
        (MAX_LINES, 2 * MAX_LINES),
        (2 * MAX_LINES, len(lines)),
    ]
    # Size counts too: three long lines do not share a part.
    long = [Line("A", "x" * 4000), Line("B", "y" * 4000), Line("A", "z" * 4000)]
    assert parts(long) == [(0, 1), (1, 2), (2, 3)]
    assert parts([]) == []

    text = request_text(lines, MAX_LINES, 2 * MAX_LINES)
    head, body = text.split("Lines to correct:\n")
    # The lines before the part are shown, and marked as not to be changed.
    assert head.startswith("Context (do not change):\n")
    assert head.count("|") == CONTEXT_LINES
    assert body.splitlines()[0] == f"{MAX_LINES + 1}| Speaker 1: line {MAX_LINES}"
    assert len(body.splitlines()) == MAX_LINES
    # The first part has nothing before it.
    assert request_text(lines, 0, 2).startswith("Lines to correct:\n1| ")


def test_clean_keeps_corrections_and_counts_rewrites() -> None:
    lines = [
        Line("Peter", "Gärna, Thomas. Tja."),
        Line("Thomas", "Ja, det är bra."),
        Line("Peter", "Det är ju så jäkla skönt ändå numera tycker jag."),
        Line("Thomas", "Ska vi ha kvar Bok Demo och läsa om plattformen?"),
        Line("Peter", "ja"),
    ]
    seen: list[str] = []

    def chat_sync(*, instructions: str, text: str):
        seen.append(text)
        return SimpleNamespace(
            text=(
                "1| Peter: Tjena, Thomas. Tja.\n"  # speaker repeated: stripped
                "3| Det är skönt numera.\n"  # a rewrite
                "4| Ska vi ha kvar Boka demo och läsa om plattformen?\n"
                "5| Ja.\n"  # punctuation and capital only
                "9| a line that does not exist\n"
            ),
            model="test-model",
        )

    steps: list[str] = []
    result = clean(SimpleNamespace(chat_sync=chat_sync), lines, on_step=steps.append)
    assert result.texts == [
        "Tjena, Thomas. Tja.",
        None,
        None,
        "Ska vi ha kvar Boka demo och läsa om plattformen?",
        "Ja.",
    ]
    assert result.rejected == 1 and result.model == "test-model"
    assert steps == ["part:1/1"] and len(seen) == 1
    assert "2| Thomas: Ja, det är bra." in seen[0]


def test_words_are_not_added_removed_or_swapped_for_other_words() -> None:
    # A word added for the grammar's sake was not said.
    assert not accept(
        "Han skulle ställa upp primärvalet tillsammans med Karolina.",
        "Han skulle ställa upp i primärvalet tillsammans med Karolina.",
    )
    # A stray word is not removed either: that is for a person to decide.
    assert not accept(
        "Det har vi ha gjort. Här är uppdaterad text.",
        "Det har vi gjort. Här är uppdaterad text.",
    )
    # A valid word swapped for one the model likes better is not a mishearing.
    assert not accept(
        "Men efter att ha förlorat kaptenens grind tog Indien bara 36 poäng.",
        "Men efter att ha förlorat kaptenens wicket tog Indien bara 36 poäng.",
    )
    # The same letters split or joined differently are the same words.
    assert accept(
        "När alla kommer ikring börjar vi bli förvånade.",
        "När alla kommer i kring börjar vi bli förvånade.",
    )


# --- the worker task ---------------------------------------------------------


def _run(monkeypatch, session, cleanup, runner):
    from contextlib import contextmanager

    from app.worker import cleanup_tasks

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(cleanup_tasks, "session_scope", _scope)
    monkeypatch.setattr("app.services.agent_runner.get_agent_runner", lambda: runner)
    return cleanup_tasks.clean_transcript(str(cleanup.id))


def _database_with_cleanup(text: str):
    from app.models.transcript_cleanup import TranscriptCleanup

    from .test_meeting_notes import _database_with_note

    # Anna Berg (directory) says ``text``; SPEAKER_01 says "Hi there."
    session, note = _database_with_note(text)
    cleanup = TranscriptCleanup(
        recording_id=note.recording_id, revision_id=note.revision_id
    )
    session.add(cleanup)
    session.commit()
    return session, cleanup


def test_task_stores_corrections_and_leaves_the_transcript_alone(monkeypatch) -> None:
    from sqlalchemy import select

    from app.models.meeting_transcript_segment import MeetingTranscriptSegment

    original = "Ska vi ha kvar Bok Demo och läsa om plattformen?"
    session, cleanup = _database_with_cleanup(original)
    seen: list[str] = []

    def chat_sync(*, instructions: str, text: str):
        seen.append(text)
        return SimpleNamespace(
            text=(
                "1| Ska vi ha kvar Boka demo och läsa om plattformen?\n"
                "2| Hello there, how are you doing today my friend?\n"  # a rewrite
            ),
            model="m",
        )

    runner = SimpleNamespace(chat_sync=chat_sync)
    assert _run(monkeypatch, session, cleanup, runner) == {
        "status": "ready",
        "changes": "1",
    }
    session.refresh(cleanup)
    assert cleanup.status == "ready" and cleanup.model == "m"
    assert cleanup.rejected == 1 and cleanup.duration_ms is not None
    segments = (
        session.execute(
            select(MeetingTranscriptSegment).order_by(MeetingTranscriptSegment.idx)
        )
        .scalars()
        .all()
    )
    assert cleanup.changes == [
        {
            "segment_id": str(segments[0].id),
            "original": original,
            "text": "Ska vi ha kvar Boka demo och läsa om plattformen?",
            "status": "suggested",
        }
    ]
    # The transcript itself is as it was.
    assert [s.text for s in segments] == [original, "Hi there."]
    assert not any(s.is_edited for s in segments)
    # The model saw names, not internal labels.
    assert f"1| Anna Berg: {original}" in seen[0]
    assert "2| SPEAKER_01: Hi there." in seen[0]
    # A duplicate delivery does nothing.
    assert _run(monkeypatch, session, cleanup, runner)["status"] == "skipped"
    session.close()


def test_task_failure_is_recorded_and_raised(monkeypatch) -> None:
    import pytest

    from app.services.agent_runner import AgentRunError

    session, cleanup = _database_with_cleanup("Hello everyone.")

    def chat_sync(*, instructions: str, text: str):
        raise AgentRunError("The agent service could not be reached.")

    with pytest.raises(AgentRunError):
        _run(monkeypatch, session, cleanup, SimpleNamespace(chat_sync=chat_sync))
    session.refresh(cleanup)
    assert cleanup.status == "failed"
    assert cleanup.error == "The agent service could not be reached."
    assert cleanup.changes == []
    session.close()
