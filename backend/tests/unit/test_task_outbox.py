"""Unit tests for the transactional task outbox + attempt reconciler (#6, A2).

The outbox row is written in the same transaction as the domain rows, so a
crash or broker outage between "commit" and "enqueue" can no longer strand a
recording in ``pending`` forever. The dispatcher drives rows to ``sent`` (or a
``dead`` letter), and the reconciler re-sends attempts whose messages were
lost (e.g. a Redis wipe) and fails attempts whose worker went silent.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.crud import task_outbox as task_outbox_crud
from app.crud import transcription_attempt as attempt_crud
from app.db.base import Base
from app.models.meeting_recording import MeetingRecording
from app.models.meeting_transcription_attempt import MeetingTranscriptionAttempt
from app.models.task_outbox import TaskOutbox
from app.models.workspace import Workspace
from app.worker import tasks as tasks_module
from app.worker.tasks import dispatch_task_outbox, reconcile_transcription_attempts

pytestmark = pytest.mark.unit


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _enqueue(session: Session, dedupe: str | None = None) -> TaskOutbox:
    row = task_outbox_crud.enqueue_sync(
        session,
        task_name="tasks.transcribe_attempt",
        queue="transcription",
        payload={"attempt_id": str(uuid.uuid4())},
        dedupe_key=dedupe,
    )
    session.commit()
    return row


def _seed_recording(session: Session, status: str = "pending") -> MeetingRecording:
    ws = Workspace(name="ws")
    session.add(ws)
    session.flush()
    rec = MeetingRecording(workspace_id=ws.id, title="m", status=status, audio_key="k1")
    session.add(rec)
    session.flush()
    return rec


def _seed_attempt(
    session: Session,
    rec: MeetingRecording,
    *,
    status: str = "pending",
    number: int = 1,
    created_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
) -> MeetingTranscriptionAttempt:
    attempt = MeetingTranscriptionAttempt(
        recording_id=rec.id,
        attempt_number=number,
        idempotency_key=f"{rec.id}:{number}",
        status=status,
        created_by_email="u@example.com",
    )
    if created_at is not None:
        attempt.created_at = created_at
    if heartbeat_at is not None:
        attempt.heartbeat_at = heartbeat_at
        attempt.started_at = heartbeat_at
    session.add(attempt)
    session.commit()
    return attempt


# ---------------------------------------------------------------- outbox crud


def test_outbox_claim_sends_and_backoff_dead_letters() -> None:
    session = _session()
    row = _enqueue(session)
    now = datetime.now(UTC)

    due = task_outbox_crud.claim_due_sync(session, now=now)
    assert [r.id for r in due] == [row.id]

    dead = task_outbox_crud.record_failure_sync(
        session,
        row,
        error="broker down",
        now=now,
        max_attempts=2,
        base_backoff_seconds=30,
    )
    assert dead is False
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at is not None and row.next_attempt_at > now
    # Not due until the backoff elapses.
    assert task_outbox_crud.claim_due_sync(session, now=now) == []

    later = row.next_attempt_at + timedelta(seconds=1)
    assert task_outbox_crud.claim_due_sync(session, now=later)
    dead = task_outbox_crud.record_failure_sync(
        session,
        row,
        error="broker down",
        now=later,
        max_attempts=2,
        base_backoff_seconds=30,
    )
    assert dead is True
    assert row.status == "dead"

    fresh = _enqueue(session)
    task_outbox_crud.mark_sent_sync(session, fresh, now=later)
    assert fresh.status == "sent"
    assert fresh.sent_at is not None
    session.close()


def test_outbox_dedupe_key_is_unique() -> None:
    session = _session()
    _enqueue(session, dedupe="attempt:x:send:1")
    dup = task_outbox_crud.enqueue_sync(
        session,
        task_name="tasks.transcribe_attempt",
        queue="transcription",
        payload={},
        dedupe_key="attempt:x:send:1",
    )
    session.commit()
    assert dup is None  # duplicate silently skipped, first row kept
    assert len(session.execute(select(TaskOutbox)).scalars().all()) == 1
    session.close()


# ---------------------------------------------------------- dispatch beat task


def _wire(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)


def test_dispatch_task_outbox_sends_and_marks_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    row = _enqueue(session)
    _wire(monkeypatch, session)
    sent: list[str] = []
    monkeypatch.setattr(
        tasks_module, "_deliver_outbox_row", lambda r: sent.append(r.task_name)
    )

    result = dispatch_task_outbox()

    assert result["sent"] == 1
    assert sent == ["tasks.transcribe_attempt"]
    session.refresh(row)
    assert row.status == "sent"
    session.close()


def test_dispatch_task_outbox_failure_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    row = _enqueue(session)
    _wire(monkeypatch, session)

    def _boom(r):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(tasks_module, "_deliver_outbox_row", _boom)
    result = dispatch_task_outbox()

    assert result["sent"] == 0
    assert result["failed"] == 1
    session.refresh(row)
    assert row.status == "pending"  # retried by the next run, after backoff
    assert row.attempts == 1
    assert row.last_error and "redis" in row.last_error
    session.close()


# ------------------------------------------------------------ reconciler task


def test_reconciler_resends_stuck_pending_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending attempt past the start timeout (its broker message was lost,
    e.g. a Redis wipe) gets a fresh outbox row — recovery does NOT depend on
    broker persistence."""
    session = _session()
    rec = _seed_recording(session)
    old = datetime.now(UTC) - timedelta(minutes=30)
    attempt = _seed_attempt(session, rec, created_at=old)
    _wire(monkeypatch, session)
    monkeypatch.setattr(tasks_module.settings, "MEETINGS_ENABLED", True)

    result = reconcile_transcription_attempts()

    assert result["resent"] == 1
    session.refresh(attempt)
    assert attempt.enqueue_count == 2
    rows = session.execute(select(TaskOutbox)).scalars().all()
    assert len(rows) == 1
    assert rows[0].dedupe_key == f"attempt:{attempt.id}:send:2"
    assert rows[0].payload == {"attempt_id": str(attempt.id)}

    # Idempotent: running again does not stack another resend for the same
    # generation (the dedupe key blocks it until the next timeout window).
    again = reconcile_transcription_attempts()
    assert again["resent"] == 1  # selected again…
    assert len(session.execute(select(TaskOutbox)).scalars().all()) == 2
    session.close()


def test_reconciler_fails_stale_running_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    rec = _seed_recording(session, status="transcribing")
    stale = datetime.now(UTC) - timedelta(hours=2)
    attempt = _seed_attempt(session, rec, status="running", heartbeat_at=stale)
    _wire(monkeypatch, session)
    monkeypatch.setattr(tasks_module.settings, "MEETINGS_ENABLED", True)

    result = reconcile_transcription_attempts()

    assert result["failed"] == 1
    session.refresh(attempt)
    session.refresh(rec)
    assert attempt.status == "failed"
    assert rec.status == "failed"
    assert rec.error  # user-visible, retryable
    session.close()


def test_reconciler_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks_module.settings, "MEETINGS_ENABLED", False)
    assert reconcile_transcription_attempts() == {"resent": 0, "failed": 0}


# ------------------------------------------------------------- attempt CAS


def test_attempt_mark_running_is_a_cas() -> None:
    session = _session()
    rec = _seed_recording(session)
    attempt = _seed_attempt(session, rec)

    assert attempt_crud.mark_running(session, attempt.id) is True
    # Duplicate delivery (acks_late redelivery / reconciler resend racing a
    # slow start) finds the attempt already claimed and must no-op.
    assert attempt_crud.mark_running(session, attempt.id) is False
    session.refresh(attempt)
    assert attempt.status == "running"
    assert attempt.started_at is not None and attempt.heartbeat_at is not None
    session.close()


def test_one_active_attempt_per_recording_is_db_enforced() -> None:
    from sqlalchemy.exc import IntegrityError

    session = _session()
    rec = _seed_recording(session)
    _seed_attempt(session, rec, status="running", number=1)
    with pytest.raises(IntegrityError):
        _seed_attempt(session, rec, status="pending", number=2)
    session.rollback()
    session.close()


def test_keyed_rows_never_dead_letter() -> None:
    """``dead_letter=False`` rows keep retrying at the backoff instead of
    going dead (review round 6): a key-bearing task's row is the liveness
    marker protecting a queued enrollment sample, and dead-lettering it
    during a long broker outage would strip protection exactly when it is
    needed most."""
    session = _session()
    row = _enqueue(session)
    now = datetime.now(UTC)

    for attempt in range(1, 6):
        dead = task_outbox_crud.record_failure_sync(
            session,
            row,
            error="broker down",
            now=now,
            max_attempts=2,  # far exceeded — still never dead
            base_backoff_seconds=30,
            dead_letter=False,
        )
        assert dead is False
        assert row.status == "pending"
        assert row.attempts == attempt
        assert row.next_attempt_at is not None
