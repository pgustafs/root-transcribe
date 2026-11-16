"""Unit tests for the meeting worker tasks (services faked at their seams).

``transcribe_attempt`` orchestrates: audio store → WAV decode → STT →
diarization → alignment → speaker identification → segment persistence. Each
external piece is stubbed exactly at the boundary the task imports, so these
tests pin the orchestration contract (statuses, persisted rows, error handling)
without torch/ffmpeg/network.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import speaker_directory
from app.services.transcript_align import SpeakerTurn, Word
from app.worker import tasks as tasks_module
from app.worker.tasks import cleanup_old_audio, enroll_voiceprint, transcribe_attempt

pytestmark = pytest.mark.unit


class _FakeStore:
    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.blobs = blobs or {}
        self.deleted: list[str] = []

    def get_sync(self, key: str) -> bytes:
        return self.blobs[key]

    def download_to_path(self, key: str, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(self.blobs[key])

    def delete_sync(self, key: str) -> None:
        self.deleted.append(key)


def _tmp_wav() -> str:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path


def _wire_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stt_result,
    turns: list[SpeakerTurn],
    content_type: str = "audio/webm",
    requested_language: str = "auto",
    speaker_embeddings: dict[str, list[float]] | None = None,
    voiceprints: list | None = None,
):
    """Stub every boundary of ``transcribe_attempt``; returns a call recorder."""
    calls: dict = {"status": [], "finish": [], "segments": None, "activity": []}
    wav_path = _tmp_wav()

    monkeypatch.setattr(
        "app.services.audio_store.get_audio_store",
        # >= media_validation.MIN_MEDIA_BYTES so the pre-decode structural guard
        # (issue #37) passes and the stubbed decode runs.
        lambda: _FakeStore({"k1": b"webm-bytes" * 40}),
    )
    monkeypatch.setattr(
        "app.services.diarization.decode_file_to_wav",
        lambda in_path, src_hint, max_seconds=None: wav_path,
    )
    monkeypatch.setattr(
        "app.services.diarization.probe_wav", lambda path: (40.0, 16_000)
    )
    monkeypatch.setattr(
        "app.services.diarization.load_waveform",
        # 40s of fake 16 kHz audio — the task derives the ground-truth duration
        # from the waveform shape.
        lambda path: {
            "waveform": SimpleNamespace(shape=(1, 640_000)),
            "sample_rate": 16_000,
        },
    )
    monkeypatch.setattr(
        "app.services.diarization.get_diarization_service",
        lambda: SimpleNamespace(diarize_sync=lambda audio: turns),
    )
    monkeypatch.setattr(
        "app.services.diarization.get_embedding_service",
        lambda: SimpleNamespace(
            embed_speakers_sync=lambda audio, t: speaker_embeddings or {}
        ),
    )

    stt_calls: list[str] = []
    calls["stt_calls"] = stt_calls

    calls["stt_languages"] = []

    def _transcribe(audio: bytes, filename: str, *, language=None):
        stt_calls.append(filename)
        calls["stt_languages"].append(language)
        if isinstance(stt_result, Exception):
            raise stt_result
        if filename in ("turn.wav", "context.wav", "pieces.wav"):
            # Per-turn fallback slice: echo a text derived from the clip marker
            # (or what a test says the model hears in that clip).
            marker = audio.decode()
            heard = calls.get("heard", {})
            text = heard.get(marker, f"turn-text-{marker}")
            return SimpleNamespace(text=text, language="en")
        return stt_result

    monkeypatch.setattr(
        "app.services.transcription.get_transcription_service",
        lambda: SimpleNamespace(
            transcribe_sync=_transcribe,
            # The full pass streams the FILE now (issue #6); route it through
            # the same recorder so tests keep one seam.
            transcribe_file_sync=lambda path, filename, **kw: _transcribe(
                b"", filename, **kw
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.diarization.slice_wav",
        lambda path, start, end, pad=0.0: f"{start:.1f}-{end:.1f}".encode(),
    )

    @contextmanager
    def _fake_scope():
        # The single-transaction completion commits/rolls back explicitly.
        yield SimpleNamespace(commit=lambda: None, rollback=lambda: None)

    monkeypatch.setattr(tasks_module, "session_scope", _fake_scope)
    # Attempt plumbing (issue #6): the task loads the attempt + recording, then
    # claims the attempt with a CAS before running the pipeline.
    rid = uuid.uuid4()
    calls["attempt_finish"] = []
    monkeypatch.setattr(
        tasks_module.transcription_attempt_crud,
        "get_sync",
        lambda s, aid: SimpleNamespace(
            id=aid,
            recording_id=rid,
            config={"filename": "m.webm"},
            created_at=datetime.now(UTC),
        ),
    )
    creator_id = uuid.uuid4()
    calls["creator_id"] = creator_id
    monkeypatch.setattr(
        tasks_module.meeting_recording_crud,
        "get_sync",
        lambda s, r: SimpleNamespace(
            id=rid,
            audio_key="k1",
            workspace_id=uuid.uuid4(),
            title="Weekly sync",
            size_bytes=1024,
            audio_content_type=content_type,
            requested_language=requested_language,
            # Recorder context the client shipped with the upload (issue #37 r7);
            # the worker attaches it to any media rejection it emits.
            client_diagnostics={"browser_family": "Chrome", "container": "webm"},
            created_by_id=creator_id,
            created_by_email="creator@example.com",
        ),
    )
    monkeypatch.setattr(
        tasks_module.transcription_attempt_crud, "mark_running", lambda s, aid: True
    )
    monkeypatch.setattr(
        tasks_module.transcription_attempt_crud,
        "heartbeat_sync",
        lambda s, aid, step=None: None,
    )
    monkeypatch.setattr(
        tasks_module.transcription_attempt_crud,
        "finish_sync",
        lambda s, aid, **kw: (
            calls["attempt_finish"].append(
                {k: v for k, v in kw.items() if k != "commit"}
            ),
            True,
        )[1],
    )
    monkeypatch.setattr(
        tasks_module.meeting_recording_crud,
        "mark_status_sync",
        lambda s, rid, status, step=None: calls["status"].append((status, step)),
    )
    monkeypatch.setattr(
        tasks_module.meeting_recording_crud,
        "finish_sync",
        lambda s, rid, **kw: calls["finish"].append(
            {k: v for k, v in kw.items() if k != "commit"}
        ),
    )

    def _fake_create_revision(s, *, recording_id, attempt_id, origin, seg_rows):
        calls["segments"] = seg_rows
        return SimpleNamespace(
            id=uuid.uuid4(), parent_revision_id=None, revision_number=1
        )

    monkeypatch.setattr(
        tasks_module.transcript_revision_crud,
        "create_revision_sync",
        _fake_create_revision,
    )
    monkeypatch.setattr(
        tasks_module.transcript_revision_crud,
        "carry_over_edits_sync",
        lambda s, rev, previous_revision_id=None: (0, 0),
    )
    # The fake session can't run the E4 auto-DLP durable enqueue.
    monkeypatch.setattr(
        tasks_module.task_outbox_crud, "enqueue_sync", lambda *a, **k: None
    )
    monkeypatch.setattr(
        tasks_module.speaker_profile_crud,
        "list_active_voiceprints_sync",
        lambda s: voiceprints or [],
    )
    # Consent + turn-quality gates (issue #6): every fixture profile is
    # consented, and the min-turn floor is lowered so the short test turns
    # still qualify as clean evidence.
    monkeypatch.setattr(
        tasks_module.speaker_consent_crud,
        "consented_profile_ids_sync",
        lambda s, purpose, workspace_id=None: {
            vp.speaker_profile_id for vp in (voiceprints or [])
        },
    )
    monkeypatch.setattr(
        tasks_module.speaker_consent_crud,
        "add_suggestion_sync",
        lambda s, **kw: calls.setdefault("suggestions", []).append(kw),
    )
    monkeypatch.setattr(tasks_module.settings, "SPEAKER_MATCH_MIN_TURN_SEC", 0.3)
    # The in-transaction activity write (review round 3) — same recorder.
    # Actor + payload captured too (review round 4: the in-tx rewrite
    # regressed attribution to "System" and nothing pinned it).
    calls["activity_rows"] = []
    monkeypatch.setattr(
        tasks_module,
        "record_activity_sync",
        lambda s, *, workspace_id, actor_id, actor_email, event_type, payload=None: (
            calls["activity"].append(event_type),
            calls["activity_rows"].append(
                {
                    "actor_id": actor_id,
                    "actor_email": actor_email,
                    "event_type": event_type,
                    "payload": payload,
                }
            ),
        ),
    )
    calls["wav_path"] = wav_path
    return calls


def test_transcribe_attempt_happy_path_with_identification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alba_id = uuid.uuid4()
    # A real, encrypted voiceprint whose centroid matches SPEAKER_00's embedding.
    vp = SimpleNamespace(
        id=uuid.uuid4(),
        speaker_profile_id=alba_id,
        embedding=speaker_directory.encrypt_embedding([1.0, 0.0]),
        dims=2,
        model_id="emb-model",
        key_id="legacy",
    )
    stt = SimpleNamespace(
        words=[Word("hello", 0.0, 0.5), Word("there", 0.6, 1.0)],
        text="hello there",
        language="en",
        duration=1.2,
        has_word_timestamps=True,
    )
    calls = _wire_pipeline(
        monkeypatch,
        stt_result=stt,
        turns=[SpeakerTurn("SPEAKER_00", 0.0, 1.0)],
        speaker_embeddings={"SPEAKER_00": [0.99, 0.01]},
        voiceprints=[vp],
    )

    result = transcribe_attempt(str(uuid.uuid4()))

    assert result["status"] == "completed"
    assert calls["attempt_finish"] == [
        {
            "status": "completed",
            "stt_model": tasks_module.settings.STT_MODEL,
            "diarization_model": tasks_module.settings.DIARIZATION_MODEL,
            "embedding_model_id": tasks_module.settings.SPEAKER_EMBEDDING_MODEL,
        }
    ]
    # Live progress: diarize first (its turns anchor attribution), then STT.
    assert calls["status"] == [
        ("diarizing", "decode"),
        ("diarizing", "diarize"),
        ("transcribing", "transcribe"),
        ("identifying", "identify"),
    ]
    assert calls["finish"] == [
        {"status": "completed", "duration_seconds": 1.2, "language": "en"}
    ]
    (seg,) = calls["segments"]
    assert seg["speaker_label"] == "SPEAKER_00"
    assert seg["speaker_profile_id"] == alba_id  # identified by voiceprint
    assert seg["speaker_confidence"] > 0.9
    assert seg["text"] == "hello there"
    assert calls["activity"] == ["meeting.transcribed"]
    # Attributed to the recording's creator, with the title preserved
    # (review round 4: the in-tx rewrite showed these events as "System").
    activity = calls["activity_rows"][0]
    assert activity["actor_id"] == calls["creator_id"]
    assert activity["actor_email"] == "creator@example.com"
    assert activity["payload"]["title"] == "Weekly sync"
    assert not os.path.exists(calls["wav_path"])  # temp WAV cleaned up


def test_transcribe_attempt_anonymous_without_matching_voiceprints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stt = SimpleNamespace(
        words=[Word("hi", 0.0, 0.4)],
        text="hi",
        language=None,
        duration=None,
        has_word_timestamps=True,
    )
    calls = _wire_pipeline(
        monkeypatch,
        stt_result=stt,
        turns=[SpeakerTurn("SPEAKER_00", 0.0, 0.5)],
        voiceprints=[],  # nobody enrolled
    )
    result = transcribe_attempt(str(uuid.uuid4()))
    assert result["status"] == "completed"
    (seg,) = calls["segments"]
    assert seg["speaker_profile_id"] is None
    assert seg["speaker_confidence"] is None


def test_transcribe_attempt_per_turn_fallback_without_word_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No word timestamps ⇒ per-turn transcription: exact turn boundaries and
    one STT call per diarized turn (plus the initial full pass)."""
    stt = SimpleNamespace(
        # Coarse segment-fallback "words" that would misattribute — must be ignored.
        words=[Word("blob of thirty seconds", 0.0, 30.0)],
        text="blob of thirty seconds",
        language="en",
        duration=30.0,
        has_word_timestamps=False,
    )
    turns = [
        SpeakerTurn("SPEAKER_00", 0.0, 9.5),
        SpeakerTurn("SPEAKER_01", 10.0, 19.5),
        SpeakerTurn("SPEAKER_00", 20.0, 29.5),
        SpeakerTurn("SPEAKER_02", 29.6, 29.7),  # <0.2s: skipped
    ]
    calls = _wire_pipeline(monkeypatch, stt_result=stt, turns=turns)

    result = transcribe_attempt(str(uuid.uuid4()))

    assert result["status"] == "completed"
    # 1 full pass + one per (non-tiny) turn.
    assert calls["stt_calls"] == ["audio.wav", "turn.wav", "turn.wav", "turn.wav"]
    segs = calls["segments"]
    assert len(segs) == 3
    # Timestamps come from the DIARIZATION turns, not the STT blob.
    assert [(s["start_ms"], s["end_ms"]) for s in segs] == [
        (0, 9500),
        (10000, 19500),
        (20000, 29500),
    ]
    assert [s["speaker_label"] for s in segs] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
    ]
    # Each segment's text is its own turn's transcription (slice marker
    # echoed). The clip is the turn plus a 0.2 s margin, never past the
    # midpoint to the next turn and never before the start of the recording.
    assert segs[0]["text"] == "turn-text-0.0-9.7"
    assert segs[1]["text"] == "turn-text-9.8-19.7"


def test_transcribe_attempt_skips_full_pass_for_large_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio above the STT upload cap (vLLM: 25 MB) must never be sent whole —
    the whole-file text comes from 30 s pieces cut in pauses instead, then the
    per-turn slices run as usual."""
    stt = SimpleNamespace(  # would only be returned by a (forbidden) full pass
        words=[], text="", language="en", duration=None, has_word_timestamps=False
    )
    calls = _wire_pipeline(
        monkeypatch,
        stt_result=stt,
        turns=[SpeakerTurn("SPEAKER_00", 0.0, 9.5)],
    )
    # Make the decoded WAV "large": any content beats a 0 MB cap.
    with open(calls["wav_path"], "wb") as f:
        f.write(b"x" * 64)
    monkeypatch.setattr(tasks_module.settings, "STT_MAX_UPLOAD_MB", 0)

    result = transcribe_attempt(str(uuid.uuid4()))

    assert result["status"] == "completed"
    # No full-file call — one piece of the whole-file text, then the turn.
    assert calls["stt_calls"] == ["window.wav", "turn.wav"]
    (seg,) = calls["segments"]
    assert seg["text"] == "turn-text-0.0-9.7"
    # Duration comes from the waveform itself (40s fake), not the STT response;
    # language comes from the first piece sent.
    assert calls["finish"] == [
        {"status": "completed", "duration_seconds": 40.0, "language": "en"}
    ]


def test_per_turn_transcription_subchunks_long_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 70s monologue turn is transcribed in 30s sub-chunks and re-joined —
    no single request may approach the upload cap."""
    stt = SimpleNamespace(
        words=[], text="", language="en", duration=70.0, has_word_timestamps=False
    )
    calls = _wire_pipeline(
        monkeypatch,
        stt_result=stt,
        turns=[SpeakerTurn("SPEAKER_00", 0.0, 70.0)],
    )
    result = transcribe_attempt(str(uuid.uuid4()))

    assert result["status"] == "completed"
    assert calls["stt_calls"] == ["audio.wav", "turn.wav", "turn.wav", "turn.wav"]
    (seg,) = calls["segments"]
    assert (seg["start_ms"], seg["end_ms"]) == (0, 70_000)
    assert seg["text"] == ("turn-text-0.0-30.0 turn-text-30.0-60.0 turn-text-60.0-70.0")


def test_a_sentence_split_by_speaker_detection_is_transcribed_in_one_piece(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two turns of one speaker 0.3 s apart are one request and one line."""
    stt = SimpleNamespace(
        words=[], text="", language="sv", duration=30.0, has_word_timestamps=False
    )
    turns = [
        SpeakerTurn("SPEAKER_00", 15.0, 19.6),
        SpeakerTurn("SPEAKER_00", 19.9, 21.5),
        SpeakerTurn("SPEAKER_01", 23.0, 24.0),
    ]
    calls = _wire_pipeline(monkeypatch, stt_result=stt, turns=turns)
    calls["heard"] = {
        "14.8-21.7": "Du, jag undrar, vet du hur långt det är mellan Stockholm "
        "och Sälen med bil?",
        "22.8-24.2": "Jag vet inte.",
    }

    assert transcribe_attempt(str(uuid.uuid4()))["status"] == "completed"

    segs = calls["segments"]
    assert [(s["speaker_label"], s["start_ms"], s["end_ms"]) for s in segs] == [
        ("SPEAKER_00", 15000, 21500),
        ("SPEAKER_01", 23000, 24000),
    ]
    assert segs[0]["text"].endswith("mellan Stockholm och Sälen med bil?")
    assert calls["stt_calls"].count("turn.wav") == 2


def test_a_sentence_the_model_skipped_in_a_turn_comes_back_from_the_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model stops after the first sentence of a clip. The whole-file text
    (plain, no timing) has the second one: it is put back, in the turn of the
    speaker who had the time to say it."""
    stt = SimpleNamespace(
        words=[],
        text="Hur mår du Gunilla idag? Jag mår bra, men lite trött. Hur mår du? "
        "Jag mår också bra.",
        language="sv",
        duration=20.0,
        has_word_timestamps=False,
    )
    turns = [
        SpeakerTurn("SPEAKER_00", 8.0, 9.8),
        SpeakerTurn("SPEAKER_01", 10.0, 12.0),
        SpeakerTurn("SPEAKER_01", 12.3, 13.6),  # joined with the turn before
        SpeakerTurn("SPEAKER_00", 14.0, 15.2),
    ]
    calls = _wire_pipeline(monkeypatch, stt_result=stt, turns=turns)
    calls["heard"] = {
        "7.8-9.9": "Hur mår du Gunilla idag?",
        "9.9-13.8": "Jag mår bra men lite trött.",
        "13.8-15.4": "Jag mår också bra.",
    }

    assert transcribe_attempt(str(uuid.uuid4()))["status"] == "completed"

    assert [(s["speaker_label"], s["text"]) for s in calls["segments"]] == [
        ("SPEAKER_00", "Hur mår du Gunilla idag?"),
        ("SPEAKER_01", "Jag mår bra men lite trött. Hur mår du?"),
        ("SPEAKER_00", "Jag mår också bra."),
    ]
    # No extra request: the whole-file text was already there.
    assert calls["stt_calls"] == ["audio.wav", "turn.wav", "turn.wav", "turn.wav"]


def test_short_turns_that_come_back_empty_are_heard_again_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short reply is too little audio for the model and comes back empty.
    It is transcribed again with context, and the neighbours' words, already
    known, are taken out: the reply is kept, under its own speaker and time."""
    stt = SimpleNamespace(
        words=[], text="", language="sv", duration=20.0, has_word_timestamps=False
    )
    turns = [
        SpeakerTurn("SPEAKER_00", 1.0, 1.6),  # "Hej!"
        SpeakerTurn("SPEAKER_01", 1.9, 2.4),  # "Hej!" (the reply: empty)
        SpeakerTurn("SPEAKER_00", 3.0, 5.5),
        SpeakerTurn("SPEAKER_01", 10.0, 12.0),
        SpeakerTurn("SPEAKER_01", 12.6, 13.4),  # "Hur mår du?" (empty)
        SpeakerTurn("SPEAKER_00", 14.0, 15.2),
        SpeakerTurn("SPEAKER_01", 17.0, 18.0),  # a cough: nothing to find
    ]
    calls = _wire_pipeline(monkeypatch, stt_result=stt, turns=turns)
    calls["heard"] = {
        "0.8-1.8": "Hej!",
        "1.8-2.6": "",
        "2.8-5.7": "Jag heter Peter. Vad heter du?",
        "9.8-12.2": "Jag mår bra men lite trött.",
        "12.4-13.6": " ",
        "13.8-15.4": "Jag mår också bra.",
        "16.8-18.2": "",
        # The same moments with 2.5 s on each side.
        "0.0-4.9": "Hej! Hej! Jag heter Peter. Vad heter",
        "10.1-15.9": "bra, men lite trött. Hur mår du? Jag mår också bra.",
        "14.5-20.5": "Jag mår också bra.",
        # Both of speaker 01's pieces at once: the model stops after the first
        # sentence. The pieces' own words are kept instead.
        "9.8-13.6": "Jag mår bra men lite trött.",
    }

    assert transcribe_attempt(str(uuid.uuid4()))["status"] == "completed"

    segs = calls["segments"]
    assert [(s["speaker_label"], s["text"]) for s in segs] == [
        ("SPEAKER_00", "Hej!"),
        ("SPEAKER_01", "Hej!"),
        ("SPEAKER_00", "Jag heter Peter. Vad heter du?"),
        # The rescued reply follows its speaker's own turn: one segment.
        ("SPEAKER_01", "Jag mår bra men lite trött. Hur mår du?"),
        ("SPEAKER_00", "Jag mår också bra."),
    ]
    # The rescued replies keep the time the diarization gave them.
    assert (segs[1]["start_ms"], segs[1]["end_ms"]) == (1900, 2400)
    assert (segs[3]["start_ms"], segs[3]["end_ms"]) == (10000, 13400)
    # One extra request per empty turn, nothing more.
    assert calls["stt_calls"].count("context.wav") == 3
    assert calls["stt_calls"].count("turn.wav") == 7
    assert calls["stt_calls"].count("pieces.wav") == 1


def test_transcribe_attempt_failure_persists_then_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-facing failed state is persisted FIRST, then the exception
    propagates so Celery records a real FAILURE (pre-#6 it returned normally
    and the ledger showed SUCCESS for a broken pipeline)."""
    calls = _wire_pipeline(
        monkeypatch,
        stt_result=RuntimeError("stt exploded"),
        turns=[],
    )
    internal = tasks_module.MEETING_MEDIA_REJECTED.labels(
        code="transcription_internal_error"
    )
    before = internal._value.get()
    with pytest.raises(RuntimeError, match="stt exploded"):
        transcribe_attempt(str(uuid.uuid4()))
    # The raw exception string is NEVER persisted or shown (issue #37): the
    # failure carries a stable code + safe message instead of str(exc).
    fin = calls["finish"][0]
    assert fin["status"] == "failed"
    assert fin["error_code"] == "transcription_internal_error"
    # F6: an internal/infra error must NOT increment the media-rejected metric.
    assert internal._value.get() == before
    assert "stt exploded" not in (fin["error"] or "")
    assert calls["attempt_finish"][0]["error_code"] == "transcription_internal_error"
    assert calls["segments"] is None  # nothing persisted
    assert calls["activity"] == ["meeting.failed"]
    activity = calls["activity_rows"][0]
    assert activity["actor_id"] == calls["creator_id"]
    assert activity["actor_email"] == "creator@example.com"
    assert activity["payload"]["title"] == "Weekly sync"
    # The failure event carries the stable code, not a raw error string.
    assert activity["payload"]["error_code"] == "transcription_internal_error"
    assert "error" not in activity["payload"]
    assert not os.path.exists(calls["wav_path"])  # cleaned up even on failure


def test_transcribe_attempt_malformed_container_is_safe_and_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-07-28 incident regression: a WebM whose media clusters precede
    the EBML header makes ffmpeg exit non-zero. decode_file_to_wav classifies +
    sanitizes it into a MediaValidationError (its own unit tests cover that); the
    worker must persist the NON-retryable code + safe message, count exactly one
    media rejection, and never reach model loading."""
    from app.services.media_validation import MediaErrorCode, MediaValidationError

    calls = _wire_pipeline(
        monkeypatch,
        stt_result=SimpleNamespace(
            words=[], text="", language="en", duration=None, has_word_timestamps=False
        ),
        turns=[SpeakerTurn("SPEAKER_00", 0.0, 0.5)],
    )

    def _boom(in_path, src_hint, max_seconds=None):
        # decode_file_to_wav now raises a typed, sanitized error (never the raw
        # CalledProcessError) — mirror that contract here.
        raise MediaValidationError(
            MediaErrorCode.INVALID_CONTAINER, detail="EBML header parsing failed"
        )

    monkeypatch.setattr("app.services.diarization.decode_file_to_wav", _boom)
    # Prove the pipeline stops at decode — a model load here would be a bug.
    monkeypatch.setattr(
        "app.services.diarization.get_diarization_service",
        lambda: (_ for _ in ()).throw(AssertionError("must not load a model")),
    )

    rejected = tasks_module.MEETING_MEDIA_REJECTED.labels(
        code="recording_invalid_container"
    )
    before = rejected._value.get()

    with pytest.raises(MediaValidationError):
        transcribe_attempt(str(uuid.uuid4()))

    fin = calls["finish"][0]
    assert fin["status"] == "failed"
    assert fin["error_code"] == "recording_invalid_container"
    # No raw stderr / paths / executable leak into the user-facing message.
    for leak in ("EBML", "ffmpeg", "/tmp/", "/opt/venv", "matroska"):
        assert leak not in (fin["error"] or "")
    assert "will not help" in fin["error"]  # tells the user a retry is futile
    assert calls["attempt_finish"][0]["error_code"] == "recording_invalid_container"
    assert calls["segments"] is None
    # F6: exactly one media rejection counted for this code.
    assert rejected._value.get() - before == 1


def test_media_rejection_log_never_carries_caller_supplied_content_type(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """`content_type` is CALLER-supplied (the upload API takes any string, and a
    multipart part declares its own), so logging it verbatim would smuggle free
    text into the operator log — bypassing the closed-token boundary entirely
    (issue #37 review round 8)."""
    import logging

    from app.services.media_validation import MediaErrorCode, MediaValidationError

    calls = _wire_pipeline(
        monkeypatch,
        stt_result=SimpleNamespace(
            words=[], text="", language="en", duration=None, has_word_timestamps=False
        ),
        turns=[SpeakerTurn("SPEAKER_00", 0.0, 0.5)],
        content_type="secret/topsecret\nforged=true",
    )
    monkeypatch.setattr(
        "app.services.diarization.decode_file_to_wav",
        lambda *a, **k: (_ for _ in ()).throw(
            MediaValidationError(MediaErrorCode.INVALID_CONTAINER, detail="bad ebml")
        ),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(MediaValidationError):
        transcribe_attempt(str(uuid.uuid4()))

    # Assert on the structured record: `extra` fields are what reach the log
    # pipeline, and they are where the raw value would have leaked.
    rejected = [
        r
        for r in caplog.records
        if getattr(r, "operation", "") == "meeting.media_rejected"
    ]
    assert rejected, "the rejection was not logged"
    record = rejected[0]
    assert not hasattr(record, "media_type")
    # Reported as a CLOSED container token instead of the caller's string.
    assert record.media_container == "unknown"
    blob = caplog.text + str(record.__dict__)
    assert "secret/topsecret" not in blob
    assert "forged=true" not in blob
    assert calls["finish"][0]["error_code"] == "recording_invalid_container"


def test_media_rejection_log_is_owned_by_the_terminal_cas(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A reclaimed attempt must not write the correlation log the runbook reads:
    the stale-heartbeat reconciler may already have failed it and started a
    retry (issue #37 review round 8)."""
    import logging

    from app.services.media_validation import MediaErrorCode, MediaValidationError

    _wire_pipeline(
        monkeypatch,
        stt_result=SimpleNamespace(
            words=[], text="", language="en", duration=None, has_word_timestamps=False
        ),
        turns=[SpeakerTurn("SPEAKER_00", 0.0, 0.5)],
    )
    monkeypatch.setattr(
        "app.services.diarization.decode_file_to_wav",
        lambda *a, **k: (_ for _ in ()).throw(
            MediaValidationError(MediaErrorCode.INVALID_CONTAINER, detail="bad ebml")
        ),
    )
    # This worker LOST the claim (the CAS returns False).
    monkeypatch.setattr(
        tasks_module.transcription_attempt_crud,
        "finish_sync",
        lambda s, aid, **kw: False,
    )

    with caplog.at_level(logging.WARNING), pytest.raises(MediaValidationError):
        transcribe_attempt(str(uuid.uuid4()))

    assert "meeting.media_rejected" not in caplog.text
    assert "rejected by media validation" not in caplog.text


def test_transcribe_attempt_duplicate_delivery_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redelivered/resent message finds the attempt already claimed (CAS
    rowcount 0) and must not re-run the pipeline."""
    stt = SimpleNamespace(
        words=[Word("hi", 0.0, 0.4)],
        text="hi",
        language=None,
        duration=None,
        has_word_timestamps=True,
    )
    calls = _wire_pipeline(
        monkeypatch, stt_result=stt, turns=[SpeakerTurn("SPEAKER_00", 0.0, 0.5)]
    )
    monkeypatch.setattr(
        tasks_module.transcription_attempt_crud, "mark_running", lambda s, aid: False
    )
    result = transcribe_attempt(str(uuid.uuid4()))
    assert result["status"] == "duplicate"
    assert calls["segments"] is None
    assert calls["status"] == []  # pipeline never started


def test_enroll_voiceprint_encrypts_persists_and_claims_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrollment commits the voiceprint WITH its idempotency claim (the
    sample row's ref_id CAS) in one transaction; a redelivery — or a
    concurrent delivery that lost the CAS — creates no duplicate print."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
    from app.worker.tasks import enroll_voiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    profile = SpeakerProfile(display_name="Anna", status="active")
    session.add(profile)
    session.flush()
    # The enrollment endpoints record identification consent before enqueuing;
    # the worker now revalidates it before processing the raw sample (issue #14
    # review), so the test must model that precondition.
    from app.models.speaker_consent import SpeakerConsentRecord

    session.add(
        SpeakerConsentRecord(
            profile_id=profile.id,
            purpose="speaker_identification",
            status="granted",
            policy_version="v1",
            actor_id=uuid.uuid4(),
            actor_email="anna@example.com",
        )
    )
    session.add(AudioObject(bucket="b", key="sample-key", kind="enrollment_sample"))
    # The sent outbox row is the sample's queued-work marker for the
    # orphan-intent sweep — enrollment must retire it WITH the result
    # (review round 4: a prune-clock deletion left queued samples sweepable).
    from app.models.task_outbox import TaskOutbox

    session.add(
        TaskOutbox(
            task_name="tasks.enroll_voiceprint",
            queue="transcription",
            payload={"profile_id": str(profile.id), "sample_key": "sample-key"},
            dedupe_key="enroll:sample-key",
            status="sent",
        )
    )
    session.commit()

    wav_path = _tmp_wav()
    monkeypatch.setattr(
        "app.services.audio_store.get_audio_store",
        lambda: _FakeStore({"sample-key": b"webm-bytes"}),
    )
    monkeypatch.setattr(
        "app.services.diarization.decode_to_wav",
        lambda audio, src_hint: wav_path,
    )
    monkeypatch.setattr(
        "app.services.diarization.load_waveform", lambda path: {"waveform": None}
    )
    monkeypatch.setattr(
        "app.services.diarization.get_embedding_service",
        lambda: SimpleNamespace(
            embed_clip_sync=lambda audio: [0.5, 0.5, 0.0], model_id="emb-model"
        ),
    )

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    result = enroll_voiceprint(str(profile.id), "sample-key", "s.webm")
    assert result["status"] == "enrolled"
    prints = session.execute(select(SpeakerVoiceprint)).scalars().all()
    assert len(prints) == 1
    assert prints[0].model_id == "emb-model"
    # No sample_retention consent → the raw sample is not kept on the print
    # and its object is tombstoned; ref_id carries the idempotency claim.
    assert prints[0].sample_key is None
    obj = session.execute(select(AudioObject)).scalars().one()
    assert obj.ref_id == prints[0].id
    assert obj.status == "delete_requested"
    # The queued-work marker was consumed in the same transaction.
    from app.models.task_outbox import TaskOutbox as _TO

    assert session.execute(select(_TO)).scalars().all() == []
    # Stored blob is encrypted, and decrypts back to the embedding.
    assert prints[0].embedding != b"\x00" * 12
    assert speaker_directory.decrypt_embedding(prints[0].embedding, 3) == pytest.approx(
        [0.5, 0.5, 0.0]
    )

    # Redelivery: the fast pre-check (or the lost CAS) skips — one print.
    result = enroll_voiceprint(str(profile.id), "sample-key", "s.webm")
    assert result["status"] == "duplicate"
    assert len(session.execute(select(SpeakerVoiceprint)).scalars().all()) == 1
    session.close()


def test_enroll_voiceprint_skips_when_identification_consent_withdrawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consent withdrawn after the 202 must stop the worker BEFORE it reads,
    decodes or embeds the raw biometric sample — no voiceprint is persisted and
    the orphaned sample is tombstoned (issue #14 review). The embedding service
    must never be called."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
    from app.worker.tasks import enroll_voiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    profile = SpeakerProfile(display_name="Gone", status="active")
    session.add(profile)
    session.flush()
    session.add(AudioObject(bucket="b", key="sample-key", kind="enrollment_sample"))
    session.commit()  # NOTE: no identification consent row → withdrawn/never

    def _boom(*a, **k):  # the sample must never be read or embedded
        raise AssertionError("processed a sample without identification consent")

    monkeypatch.setattr("app.services.audio_store.get_audio_store", _boom)
    monkeypatch.setattr("app.services.diarization.get_embedding_service", _boom)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    result = enroll_voiceprint(str(profile.id), "sample-key", "s.webm")
    assert result["status"] == "skipped"
    assert result["reason"] == "identification_consent_revoked"
    assert session.execute(select(SpeakerVoiceprint)).scalars().all() == []
    # The unprocessable sample is tombstoned, not left as active raw audio.
    obj = session.execute(select(AudioObject)).scalars().one()
    assert obj.status == "delete_requested"
    session.close()


def test_enroll_voiceprint_skips_when_profile_deactivated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deactivated (revoked) profile must not have a sample processed into a
    voiceprint (issue #14 review), even with identification consent granted."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
    from app.worker.tasks import enroll_voiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    profile = SpeakerProfile(display_name="Revoked", status="revoked")
    session.add(profile)
    session.flush()
    session.add(
        SpeakerConsentRecord(
            profile_id=profile.id,
            purpose="speaker_identification",
            status="granted",
            policy_version="v1",
            actor_id=uuid.uuid4(),
            actor_email="r@example.com",
        )
    )
    session.add(AudioObject(bucket="b", key="sample-key", kind="enrollment_sample"))
    session.commit()

    def _boom(*a, **k):
        raise AssertionError("processed a sample for a deactivated profile")

    monkeypatch.setattr("app.services.audio_store.get_audio_store", _boom)
    monkeypatch.setattr("app.services.diarization.get_embedding_service", _boom)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    result = enroll_voiceprint(str(profile.id), "sample-key", "s.webm")
    assert result["status"] == "skipped"
    assert result["reason"] == "profile_deactivated"
    assert session.execute(select(SpeakerVoiceprint)).scalars().all() == []
    session.close()


def test_enroll_voiceprint_skips_when_profile_erased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile fully erased before the worker runs is reported as
    `profile_erased`, NOT `profile_deactivated` (issue #14 review): re-enrolling
    a non-existent profile is not the guidance, and the sample is tombstoned."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.speaker_profile import SpeakerVoiceprint
    from app.worker.tasks import enroll_voiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    # No SpeakerProfile row — it was erased.
    session.add(AudioObject(bucket="b", key="sample-key", kind="enrollment_sample"))
    session.commit()

    def _boom(*a, **k):
        raise AssertionError("processed a sample for an erased profile")

    monkeypatch.setattr("app.services.audio_store.get_audio_store", _boom)
    monkeypatch.setattr("app.services.diarization.get_embedding_service", _boom)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    result = enroll_voiceprint(str(uuid.uuid4()), "sample-key", "s.webm")
    assert result["status"] == "skipped"
    assert result["reason"] == "profile_erased"
    assert session.execute(select(SpeakerVoiceprint)).scalars().all() == []
    obj = session.execute(select(AudioObject)).scalar_one()
    assert obj.status == "delete_requested"
    session.close()


def test_enroll_voiceprint_skips_when_sample_already_tombstoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLAIM: a sample tombstoned for deletion BEFORE the worker starts is
    seen under the lifecycle-row lock, so no raw read occurs (issue #14 review).
    The store/embedding must never be touched."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
    from app.worker.tasks import enroll_voiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    profile = SpeakerProfile(display_name="Tomb", status="active")
    session.add(profile)
    session.flush()
    session.add(
        SpeakerConsentRecord(
            profile_id=profile.id,
            purpose="speaker_identification",
            status="granted",
            policy_version="v1",
            actor_id=uuid.uuid4(),
            actor_email="t@example.com",
        )
    )
    # Already tombstoned (a withdrawal/erasure committed first).
    session.add(
        AudioObject(
            bucket="b",
            key="sample-key",
            kind="enrollment_sample",
            status="delete_requested",
        )
    )
    session.commit()

    def _boom(*a, **k):
        raise AssertionError("read/embedded a sample already pending deletion")

    monkeypatch.setattr("app.services.audio_store.get_audio_store", _boom)
    monkeypatch.setattr("app.services.diarization.get_embedding_service", _boom)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    result = enroll_voiceprint(str(profile.id), "sample-key", "s.webm")
    assert result["status"] == "skipped"
    assert result["reason"] == "sample_unavailable"
    assert session.execute(select(SpeakerVoiceprint)).scalars().all() == []
    session.close()


def test_enroll_voiceprint_withdrawal_between_read_and_persist_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's TOCTOU schedule: consent live at the authorized read, but
    WITHDRAWN before persistence. The post-lock recheck must catch it — no
    voiceprint is stored and the sample is tombstoned (issue #14 review)."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
    from app.worker.tasks import enroll_voiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    profile = SpeakerProfile(display_name="Race", status="active")
    session.add(profile)
    session.flush()
    session.add(
        SpeakerConsentRecord(
            profile_id=profile.id,
            purpose="speaker_identification",
            status="granted",
            policy_version="v1",
            actor_id=uuid.uuid4(),
            actor_email="race@example.com",
            created_at=datetime.now(UTC) - timedelta(seconds=30),
        )
    )
    session.add(AudioObject(bucket="b", key="sample-key", kind="enrollment_sample"))
    session.commit()

    wav_path = _tmp_wav()
    monkeypatch.setattr(
        "app.services.audio_store.get_audio_store",
        lambda: _FakeStore({"sample-key": b"webm-bytes"}),
    )
    monkeypatch.setattr(
        "app.services.diarization.decode_to_wav", lambda audio, src_hint: wav_path
    )
    monkeypatch.setattr(
        "app.services.diarization.load_waveform", lambda path: {"waveform": None}
    )

    def _embed_then_withdraw(_waveform):
        # A concurrent withdrawal lands AFTER the authorized read, BEFORE persist.
        session.add(
            SpeakerConsentRecord(
                profile_id=profile.id,
                purpose="speaker_identification",
                status="withdrawn",
                policy_version="v1",
                actor_id=uuid.uuid4(),
                actor_email="race@example.com",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
        return [0.5, 0.5, 0.0]

    monkeypatch.setattr(
        "app.services.diarization.get_embedding_service",
        lambda: SimpleNamespace(
            embed_clip_sync=_embed_then_withdraw, model_id="emb-model"
        ),
    )

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    result = enroll_voiceprint(str(profile.id), "sample-key", "s.webm")
    assert result["status"] == "skipped"
    assert result["reason"] == "identification_consent_revoked"
    assert session.execute(select(SpeakerVoiceprint)).scalars().all() == []
    obj = session.execute(select(AudioObject)).scalars().one()
    assert obj.status == "delete_requested"
    session.close()


def test_enroll_voiceprint_failure_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrollment failures re-raise so Celery records FAILURE (issue #6)."""
    monkeypatch.setattr(
        "app.services.audio_store.get_audio_store",
        lambda: _FakeStore({}),  # sample missing -> KeyError inside the task
    )

    @contextmanager
    def _scope():
        # The idempotency pre-check sees no lifecycle row for the sample.
        yield SimpleNamespace(
            get=lambda *a, **k: None, commit=lambda: None, rollback=lambda: None
        )

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    # The claim locks the PROFILE and returns the (active) row.
    monkeypatch.setattr(
        tasks_module.speaker_profile_crud,
        "lock_for_update_sync",
        lambda s, pid: SimpleNamespace(status="active"),
    )
    # An ACTIVE lifecycle row exists (fail-closed passes) so the worker reaches
    # the (failing) store read rather than skipping.
    _active = SimpleNamespace(ref_id=None, status="active")
    monkeypatch.setattr(
        tasks_module.audio_object_crud, "get_by_key_sync", lambda s, k: _active
    )
    monkeypatch.setattr(
        tasks_module.audio_object_crud,
        "ensure_for_key_sync",
        lambda s, **kw: SimpleNamespace(ref_id=None),
    )
    monkeypatch.setattr(
        tasks_module.audio_object_crud,
        "request_delete_sync",
        lambda s, o, **kw: None,
    )
    # Identification consent is live, so the worker proceeds to the (failing)
    # store read rather than skipping.
    monkeypatch.setattr(
        tasks_module.speaker_consent_crud,
        "current_record_sync",
        lambda s, **kw: SimpleNamespace(status="granted", expires_at=None),
    )
    with pytest.raises(KeyError):
        enroll_voiceprint(str(uuid.uuid4()), "missing", "s.webm")


def test_cleanup_old_audio_requests_deletion_without_claiming_erasure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retention sweep only *requests* durable deletion (issue #6, wave A1).

    It must never delete inline-and-suppress, and it must never clear the object
    key or flip ``audio_deleted`` — that happens only after the deletion sweep
    CONFIRMS the store delete.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.meeting_recording import MeetingRecording
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
    from app.models.workspace import Workspace

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    ws = Workspace(name="ws")
    session.add(ws)
    session.flush()
    old = MeetingRecording(
        workspace_id=ws.id,
        title="old",
        status="completed",
        audio_key="k-old",
        created_at=datetime.now(UTC) - timedelta(days=120),
    )
    fresh = MeetingRecording(
        workspace_id=ws.id, title="fresh", status="completed", audio_key="k-new"
    )
    expired_profile = SpeakerProfile(
        display_name="Gone",
        status="active",
        consent_granted_at=datetime.now(UTC),
        retention_delete_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add_all([old, fresh, expired_profile])
    session.flush()
    session.add(
        SpeakerVoiceprint(
            speaker_profile_id=expired_profile.id,
            embedding=b"cipher",
            dims=2,
            model_id="m",
            sample_key="sample-1",
        )
    )
    session.commit()

    store = _FakeStore()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: store)
    monkeypatch.setattr(tasks_module.settings, "MEETINGS_ENABLED", True)
    monkeypatch.setattr(tasks_module.settings, "AUDIO_RETENTION_DAYS", 90)

    result = cleanup_old_audio()

    assert result == {"audio_delete_requested": 2, "voiceprints_deleted": 1}
    # Nothing is deleted inline any more — the durable sweep owns store calls.
    assert store.deleted == []
    session.refresh(old)
    assert old.audio_deleted is False and old.audio_key == "k-old"
    session.refresh(fresh)
    assert fresh.audio_deleted is False  # untouched
    # The voiceprint row (encrypted embedding) is erased immediately; its sample
    # object deletion is tracked durably instead of best-effort.
    assert session.query(SpeakerVoiceprint).count() == 0
    tombstones = {
        o.key: o
        for o in session.execute(select(AudioObject)).scalars()
        if o.status == "delete_requested"
    }
    assert set(tombstones) == {"k-old", "sample-1"}
    assert tombstones["k-old"].kind == "meeting_recording"
    assert tombstones["k-old"].delete_reason == "retention"
    assert tombstones["sample-1"].kind == "enrollment_sample"

    # Re-running the sweep is idempotent: the aged recording is re-selected (its
    # request is a no-op), the already-erased voiceprints are not.
    again = cleanup_old_audio()
    assert again == {"audio_delete_requested": 1, "voiceprints_deleted": 0}
    session.close()


def test_cleanup_old_audio_still_purges_expired_data_when_features_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention OUTLIVES enablement (issue #14 review): an expired voiceprint
    must still be erased after the feature is switched off — obligations do not
    vanish with the flag. (Was a hard no-op, which pinned the unsafe behavior.)"""
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    profile = SpeakerProfile(
        display_name="Gone",
        status="active",
        consent_granted_at=datetime.now(UTC),
        retention_delete_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(profile)
    session.flush()
    session.add(
        SpeakerVoiceprint(
            speaker_profile_id=profile.id, embedding=b"c", dims=1, model_id="m"
        )
    )
    session.commit()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr(tasks_module.settings, "MEETINGS_ENABLED", False)

    result = cleanup_old_audio()
    assert result["voiceprints_deleted"] == 1
    assert session.query(SpeakerVoiceprint).count() == 0
    session.close()


def test_cleanup_purges_sample_when_retention_consent_lapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained sample whose sample_retention/voice_synthesis consent is no
    longer live (expired or withdrawn) is tombstoned by the sweep even without
    a profile retention date (issue #14 review) — expiry must not leave raw
    biometric audio lying around."""
    from contextlib import contextmanager

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    profile = SpeakerProfile(
        display_name="Lapsed", status="active", consent_granted_at=datetime.now(UTC)
    )
    session.add(profile)
    session.flush()
    session.add(
        SpeakerVoiceprint(
            speaker_profile_id=profile.id,
            embedding=b"c",
            dims=1,
            model_id="m",
            sample_key="lapsed-sample",
        )
    )
    # A voice_synthesis grant that has already EXPIRED — no live retention.
    session.add(
        SpeakerConsentRecord(
            profile_id=profile.id,
            purpose="voice_synthesis",
            status="granted",
            policy_version="v1",
            actor_id=uuid.uuid4(),
            actor_email="u@example.com",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    session.commit()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr(
        "app.services.audio_store.get_audio_store", lambda: _FakeStore()
    )

    cleanup_old_audio()
    # The voiceprint row survives (identification is a distinct purpose); only
    # the raw sample is tombstoned. And sample_key is NOT cleared pre-confirm.
    vp = session.query(SpeakerVoiceprint).one()
    assert vp.sample_key == "lapsed-sample"
    obj = (
        session.execute(select(AudioObject).where(AudioObject.key == "lapsed-sample"))
    ).scalar_one()
    assert obj.status == "delete_requested"
    session.close()


def test_cleanup_keeps_sample_with_a_live_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expiry backstop re-checks consent per profile and must NOT purge a
    sample whose grant is still live — a renewal must survive the sweep
    (issue #14 review)."""
    from contextlib import contextmanager

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    profile = SpeakerProfile(
        display_name="Kept", status="active", consent_granted_at=datetime.now(UTC)
    )
    session.add(profile)
    session.flush()
    session.add(
        SpeakerVoiceprint(
            speaker_profile_id=profile.id,
            embedding=b"c",
            dims=1,
            model_id="m",
            sample_key="kept-sample",
        )
    )
    session.add(
        SpeakerConsentRecord(
            profile_id=profile.id,
            purpose="sample_retention",
            status="granted",
            policy_version="v1",
            actor_id=uuid.uuid4(),
            actor_email="u@example.com",
        )
    )
    session.commit()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr(
        "app.services.audio_store.get_audio_store", lambda: _FakeStore()
    )

    cleanup_old_audio()
    # No deletion request: the live grant is respected.
    tombstones = [
        o
        for o in session.execute(select(AudioObject)).scalars()
        if o.status != "active"
    ]
    assert tombstones == []
    session.close()


def test_retention_resolution_and_hold_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-workspace retention overrides + legal holds drive the sweep (E3)."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.meeting_governance import LegalHold, RetentionPolicy
    from app.models.meeting_recording import MeetingRecording
    from app.models.workspace import Workspace
    from app.services import retention

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    ws_short = Workspace(name="short-retention")
    ws_default = Workspace(name="default-retention")
    session.add_all([ws_short, ws_default])
    session.flush()
    session.add(
        RetentionPolicy(
            scope_type="workspace",
            scope_id=ws_short.id,
            artifact="raw_audio",
            retention_days=7,
        )
    )
    thirty_days_old = datetime.now(UTC) - timedelta(days=30)
    aged_in_short = MeetingRecording(
        workspace_id=ws_short.id,
        title="over 7d override",
        status="completed",
        audio_key="k-short",
        created_at=thirty_days_old,
    )
    fresh_in_default = MeetingRecording(
        workspace_id=ws_default.id,
        title="inside 90d default",
        status="completed",
        audio_key="k-default",
        created_at=thirty_days_old,
    )
    held = MeetingRecording(
        workspace_id=ws_short.id,
        title="held",
        status="completed",
        audio_key="k-held",
        created_at=thirty_days_old,
    )
    session.add_all([aged_in_short, fresh_in_default, held])
    session.flush()
    session.add(LegalHold(scope_type="recording", scope_id=held.id, reason="LIT-42"))
    session.commit()

    # Resolution: workspace override beats the settings default.
    assert (
        retention.resolve_retention_days_sync(
            session, artifact="raw_audio", workspace_id=ws_short.id
        )
        == 7
    )
    assert (
        retention.resolve_retention_days_sync(
            session, artifact="raw_audio", workspace_id=ws_default.id
        )
        == 90
    )

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr(
        "app.services.audio_store.get_audio_store", lambda: _FakeStore()
    )
    monkeypatch.setattr(tasks_module.settings, "MEETINGS_ENABLED", True)

    result = cleanup_old_audio()

    # Only the aged, un-held recording in the short-retention workspace was
    # requested; the held one and the inside-default one were skipped.
    assert result["audio_delete_requested"] == 1
    requested = (
        session.execute(
            select(AudioObject).where(AudioObject.status == "delete_requested")
        )
        .scalars()
        .all()
    )
    assert [o.key for o in requested] == ["k-short"]
    session.close()


def test_create_meeting_clip_slices_and_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clip task CAS-claims, runs ffmpeg file→file, uploads the slice as a
    clip audio_object, and no-ops on duplicate delivery; ffmpeg failure
    persists a user-visible error and re-raises."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.meeting_clip import MeetingClip
    from app.models.meeting_recording import MeetingRecording
    from app.models.workspace import Workspace
    from app.worker.tasks import create_meeting_clip

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    ws = Workspace(name="ws")
    session.add(ws)
    session.flush()
    rec = MeetingRecording(
        workspace_id=ws.id, title="Source", status="completed", audio_key="k-src"
    )
    session.add(rec)
    session.flush()
    clip = MeetingClip(
        workspace_id=ws.id,
        recording_id=rec.id,
        title="Highlight",
        start_ms=1000,
        end_ms=3000,
        status="pending",
    )
    session.add(clip)
    session.commit()

    store = _FakeStore({"k-src": b"source-bytes"})
    uploaded: dict[str, bytes] = {}

    def fake_put_file(key: str, fileobj, content_type: str) -> str:
        uploaded[key] = fileobj.read()
        return key

    store.put_file = fake_put_file  # type: ignore[attr-defined]

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: store)
    # A non-empty AUDIO_S3_KEY_PREFIX must namespace the clip key (issue #16
    # review: the prefix has to isolate ALL key kinds, not just recordings).
    from app.core.config import settings as _settings

    monkeypatch.setattr(_settings, "AUDIO_S3_KEY_PREFIX", "tenant-a")
    import sys

    # imageio_ffmpeg is an [stt]-only dep; the task imports it lazily.
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: "/fake/ffmpeg"),
    )

    def fake_run(cmd, capture_output=True, timeout=0):
        # The output path is the last argument; write the "sliced" audio.
        with open(cmd[-1], "wb") as fh:
            fh.write(b"sliced-opus")
        assert "-ss" in cmd and "-to" in cmd
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = create_meeting_clip(str(clip.id))
    assert result["status"] == "ready"
    session.refresh(clip)
    assert clip.status == "ready"
    assert clip.audio_key and clip.audio_key.endswith(".webm")
    # Prefix isolation: the clip key routes through key_namespace().
    assert clip.audio_key.startswith("tenant-a/workspaces/")
    assert "/clips/" in clip.audio_key
    assert uploaded[clip.audio_key] == b"sliced-opus"
    objs = session.execute(select(AudioObject)).scalars().all()
    assert [o.kind for o in objs] == ["clip"]
    assert objs[0].size_bytes == len(b"sliced-opus")

    # Duplicate delivery no-ops.
    assert create_meeting_clip(str(clip.id)) == {
        "status": "skipped",
        "reason": "not_pending",
    }

    # Failure path: ffmpeg error persists on the row AND raises.
    clip2 = MeetingClip(
        workspace_id=ws.id,
        recording_id=rec.id,
        title="Broken",
        start_ms=0,
        end_ms=1000,
        status="pending",
    )
    session.add(clip2)
    session.commit()

    def failing_run(cmd, capture_output=True, timeout=0):
        return SimpleNamespace(returncode=1, stderr=b"boom: bad stream")

    monkeypatch.setattr("subprocess.run", failing_run)
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        create_meeting_clip(str(clip2.id))
    session.refresh(clip2)
    assert clip2.status == "failed"
    assert "boom" in clip2.error
    session.close()


def test_dlp_scan_and_redaction_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scan proposes findings idempotently; redaction produces a superseding
    masked APPROVED revision, a muted rendition swap, and a tombstone on the
    original object — never an in-place edit or inline delete."""
    import sys

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.meeting_dlp import DlpFinding, DlpTerm
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_transcript_revision import MeetingTranscriptRevision
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.models.workspace import Workspace
    from app.worker.tasks import apply_meeting_redaction, scan_meeting_dlp

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    ws = Workspace(name="ws")
    session.add(ws)
    session.flush()
    rec = MeetingRecording(
        workspace_id=ws.id, title="Sensitive", status="completed", audio_key="k-src"
    )
    session.add(rec)
    session.flush()
    revision = MeetingTranscriptRevision(
        recording_id=rec.id, revision_number=1, state="approved"
    )
    session.add(revision)
    session.flush()
    seg = MeetingTranscriptSegment(
        recording_id=rec.id,
        revision_id=revision.id,
        idx=0,
        start_ms=5_000,
        end_ms=9_000,
        speaker_label="SPEAKER_00",
        text="Reach me at anna@example.com about Project Falcon.",
    )
    session.add(seg)
    session.add(DlpTerm(workspace_id=ws.id, pattern="Project Falcon", label="codename"))
    session.add(AudioObject(bucket="b", key="k-src", kind="meeting_recording"))
    rec.approved_revision_id = revision.id
    rec.latest_revision_id = revision.id
    session.commit()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    # Scan: one email + one term finding; a rescan doubles nothing.
    result = scan_meeting_dlp(str(rec.id), str(revision.id))
    assert result["findings"] == "2"
    scan_meeting_dlp(str(rec.id), str(revision.id))
    findings = session.execute(select(DlpFinding)).scalars().all()
    assert sorted(f.detector for f in findings) == ["email", "term"]

    # Accept both; wire the fake store + ffmpeg; apply.
    for f in findings:
        f.status = "accepted"
    session.commit()

    store = _FakeStore({"k-src": b"source-bytes"})
    uploaded: dict[str, bytes] = {}

    def fake_put_file(key: str, fileobj, content_type: str) -> str:
        uploaded[key] = fileobj.read()
        return key

    store.put_file = fake_put_file  # type: ignore[attr-defined]
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: store)
    # Prefix isolation for the redacted rendition (issue #16 review).
    from app.core.config import settings as _settings

    monkeypatch.setattr(_settings, "AUDIO_S3_KEY_PREFIX", "tenant-a")
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: "/fake/ffmpeg"),
    )
    captured_cmd: list[str] = []

    def fake_run(cmd, capture_output=True, timeout=0):
        captured_cmd.extend(cmd)
        with open(cmd[-1], "wb") as fh:
            fh.write(b"muted-audio")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = apply_meeting_redaction(str(rec.id))
    assert result["status"] == "completed"

    # The mute filter covers the segment's time range.
    volume_filter = captured_cmd[captured_cmd.index("-af") + 1]
    assert "between(t,5.000,9.000)" in volume_filter

    session.refresh(rec)
    session.refresh(revision)
    new_revision = session.get(MeetingTranscriptRevision, rec.approved_revision_id)
    assert new_revision is not None and new_revision.id != revision.id
    assert new_revision.origin == "redaction"
    assert new_revision.state == "approved"
    assert revision.state == "superseded"
    new_seg = (
        session.execute(
            select(MeetingTranscriptSegment).where(
                MeetingTranscriptSegment.revision_id == new_revision.id
            )
        )
        .scalars()
        .one()
    )
    assert "anna@example.com" not in new_seg.text
    assert "Project Falcon" not in new_seg.text
    assert "Reach me at" in new_seg.text  # untouched text keeps its offsets
    assert len(new_seg.text) == len(seg.text)

    # Object swap + honest tombstone on the original.
    assert rec.audio_key in uploaded
    assert rec.audio_key.startswith("tenant-a/workspaces/")
    assert "/redacted/" in rec.audio_key
    old_obj = session.execute(
        select(AudioObject).where(AudioObject.key == "k-src")
    ).scalar_one()
    assert old_obj.status == "delete_requested"
    assert all(f.status == "redacted" for f in findings)
    session.close()


def test_build_compliance_export_bundles_subject_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DSAR bundle carries consents, voiceprint METADATA (never the
    embedding), appearances, deletion evidence, and audit events."""
    import io
    import json
    import zipfile

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.compliance_export import ComplianceExportJob
    from app.models.meeting_governance import MeetingAuditEvent
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_transcript_revision import MeetingTranscriptRevision
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
    from app.models.user import User
    from app.models.workspace import Workspace
    from app.worker.tasks import build_compliance_export

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    subject = User(email="subject@example.com", hashed_password="x")
    ws = Workspace(name="ws")
    session.add_all([subject, ws])
    session.flush()
    profile = SpeakerProfile(
        display_name="Subject", user_id=subject.id, status="active"
    )
    session.add(profile)
    session.flush()
    session.add(
        SpeakerVoiceprint(
            speaker_profile_id=profile.id,
            embedding=b"\x00" * 16,
            dims=4,
            model_id="model-x",
            sample_key="samples/s1.webm",
        )
    )
    session.add(
        SpeakerConsentRecord(
            profile_id=profile.id,
            purpose="speaker_identification",
            status="granted",
            policy_version="v1",
        )
    )
    rec = MeetingRecording(
        workspace_id=ws.id, title="Weekly", status="completed", audio_key="k1"
    )
    session.add(rec)
    session.flush()
    revision = MeetingTranscriptRevision(
        recording_id=rec.id, revision_number=1, state="approved"
    )
    session.add(revision)
    session.flush()
    session.add(
        MeetingTranscriptSegment(
            recording_id=rec.id,
            revision_id=revision.id,
            idx=0,
            start_ms=1000,
            end_ms=4000,
            speaker_label="SPEAKER_00",
            speaker_profile_id=profile.id,
            text="Hi.",
        )
    )
    session.add(
        AudioObject(
            bucket="b",
            key="samples/s1.webm",
            kind="enrollment_sample",
            status="deleted",
            delete_reason="user_requested",
            total_delete_attempts=3,
            deleted_version_ids=["v1", "v2"],
        )
    )
    session.add(
        MeetingAuditEvent(
            workspace_id=ws.id, actor_email="subject@example.com", action="audio.listen"
        )
    )
    job = ComplianceExportJob(subject_email="subject@example.com")
    session.add(job)
    session.commit()

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)
    uploaded: dict[str, bytes] = {}
    store = _FakeStore()
    store.put_file = lambda key, fileobj, ct: uploaded.__setitem__(  # type: ignore[attr-defined]
        key, fileobj.read()
    )
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: store)
    # Prefix isolation for the DSAR export bundle key (issue #16 review).
    from app.core.config import settings as _settings

    monkeypatch.setattr(_settings, "AUDIO_S3_KEY_PREFIX", "tenant-a")

    result = build_compliance_export(str(job.id))
    assert result["status"] == "completed"
    session.refresh(job)
    assert job.status == "completed"
    assert job.audio_key in uploaded
    assert job.audio_key.startswith("tenant-a/compliance/")

    with zipfile.ZipFile(io.BytesIO(uploaded[job.audio_key])) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        consents = json.loads(zf.read("consents.json"))
        assert consents[0]["purpose"] == "speaker_identification"
        vps = json.loads(zf.read("voiceprints.json"))
        assert vps[0]["model_id"] == "model-x"
        assert "embedding" not in vps[0]  # the biometric never leaves
        appearances = json.loads(zf.read("appearances.json"))
        assert appearances[0]["title"] == "Weekly"
        assert appearances[0]["segments"] == 1
        evidence = json.loads(zf.read("deletion_evidence.json"))
        deleted = next(e for e in evidence if e["status"] == "deleted")
        # The cumulative audit counter and accumulated version ids are exported
        # as erasure evidence (issue #14 review).
        assert deleted["total_delete_attempts"] == 3
        assert deleted["deleted_version_ids"] == ["v1", "v2"]
        audit = json.loads(zf.read("audit_events.json"))
        assert audit[0]["action"] == "audio.listen"

    # Duplicate delivery no-ops.
    assert build_compliance_export(str(job.id)) == {
        "status": "skipped",
        "reason": "not_pending",
    }
    session.close()


def test_multipart_reconciliation_aborts_only_owned_stale_uploads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bucket-listing sweep must only abort uploads THIS deployment can
    attribute to itself by EXACT key membership in its own rows (review round
    5: workspace-uuid parsing misattributes across database clones)."""
    from datetime import timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.worker.tasks import expire_upload_sessions

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    ws = uuid.uuid4()
    owned_stale = f"workspaces/{ws}/a.webm"
    owned_unknown_age = f"workspaces/{ws}/c.webm"
    # Same workspace uuid (a database clone shares those) but a key that has
    # no row here — the OTHER deployment's upload.
    foreign = f"workspaces/{ws}/b.webm"
    session.add_all(
        [
            AudioObject(bucket="b", key=owned_stale, kind="meeting_recording"),
            AudioObject(bucket="b", key=owned_unknown_age, kind="meeting_recording"),
        ]
    )
    session.commit()

    stale = datetime.now(UTC) - timedelta(hours=48)
    aborted: list[str] = []
    store = SimpleNamespace(
        iter_multipart_uploads=lambda *, prefix="": iter(
            [
                # Ours (key row exists) + stale ⇒ the one legitimate abort.
                {"key": owned_stale, "upload_id": "u1", "initiated": stale},
                # No row for the key ⇒ NEVER aborted, even under our prefix.
                {"key": foreign, "upload_id": "u2", "initiated": stale},
                # Ours but unknown age ⇒ not provably stale, skipped.
                {"key": owned_unknown_age, "upload_id": "u3", "initiated": None},
            ]
        ),
        abort_multipart=lambda key, upload_id: (aborted.append(upload_id), True)[1],
        list_multipart_parts=lambda key, upload_id: [],
    )
    monkeypatch.setattr(tasks_module.settings, "AUDIO_S3_ENDPOINT_URL", "http://s4")
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: store)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    result = expire_upload_sessions()
    assert aborted == ["u1"]
    assert result == {"expired": 1}
    session.close()


def test_expire_upload_sessions_closes_null_upload_id_intents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An intent-first session whose s3_upload_id was never stamped (crash in
    the create gap) must still expire — there is nothing store-side to abort
    by id, and leaving it "active" would shield its key from reconciliation
    forever (review round 5). Expiry deletes the uploading recording, whose
    CASCADE (enforced here like PostgreSQL enforces it — review round 6: the
    earlier test ran with SQLite FKs off and masked the evidence loss) takes
    the session row too, so a durable audio_object tombstone must carry the
    key's ownership onward into the deletion sweep."""
    from datetime import timedelta

    from sqlalchemy import create_engine, event, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_upload_session import MeetingUploadSession
    from app.models.workspace import Workspace
    from app.worker.tasks import expire_upload_sessions

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # match production FK enforcement
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = Session(engine)

    ws = Workspace(name="w")
    session.add(ws)
    session.flush()
    rec = MeetingRecording(
        workspace_id=ws.id,
        title="stuck",
        status="uploading",
        audio_key="workspaces/k/stuck.webm",
        audio_content_type="audio/webm",
    )
    session.add(rec)
    session.flush()
    session.add(
        MeetingUploadSession(
            recording_id=rec.id,
            workspace_id=ws.id,
            key="workspaces/k/stuck.webm",
            s3_upload_id=None,
            part_size_bytes=8,
            filename="stuck.webm",
            content_type="audio/webm",
            status="active",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    session.commit()

    aborts: list[str] = []
    store = SimpleNamespace(
        iter_multipart_uploads=lambda *, prefix="": iter([]),
        abort_multipart=lambda key, upload_id: (aborts.append(upload_id), True)[1],
        list_multipart_parts=lambda key, upload_id: [],
    )
    monkeypatch.setattr(tasks_module.settings, "AUDIO_S3_ENDPOINT_URL", "http://s4")
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: store)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    result = expire_upload_sessions()
    assert result == {"expired": 1}
    assert aborts == []  # nothing addressable — no bogus abort(None) call
    # The recording shell is gone and its CASCADE took the session row —
    # exactly as PostgreSQL behaves in production.
    assert session.execute(select(MeetingRecording)).scalars().all() == []
    assert session.execute(select(MeetingUploadSession)).scalars().all() == []
    # Ownership evidence survives as a durable tombstone: the deletion sweep
    # will confirm the (absent) object AND abort any store-side upload of
    # this key from the create gap.
    tomb = session.execute(select(AudioObject)).scalars().one()
    assert tomb.key == "workspaces/k/stuck.webm"
    assert tomb.status == "delete_requested"
    assert tomb.delete_reason == "expired_upload_intent"
    session.close()


def test_fence_stops_live_sessions_before_their_audio_is_erased() -> None:
    """Erasure closes the tap first (issue #37 review round 17).

    Aborting a multipart upload does not stop a client mid-upload: every new
    part re-creates the condition erasure must prove absent, so the store's
    abort/ListParts loop could never converge and erasure would raise forever.
    The fence moves any LIVE session for the key to `expiring`, which the part
    endpoint refuses — leaving only the already-in-flight writes for the loop
    to settle. Terminal sessions are left exactly as they are.
    """
    from datetime import timedelta

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.crud import meeting_upload as meeting_upload_crud
    from app.db.base import Base
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_upload_session import MeetingUploadSession
    from app.models.workspace import Workspace

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    ws = Workspace(name="w")
    session.add(ws)
    session.flush()

    def _add(status: str, key: str) -> uuid.UUID:
        rec = MeetingRecording(workspace_id=ws.id, title=status, status="uploading")
        session.add(rec)
        session.flush()
        row = MeetingUploadSession(
            recording_id=rec.id,
            workspace_id=ws.id,
            key=key,
            s3_upload_id="mp",
            part_size_bytes=8,
            filename="m.webm",
            content_type="audio/webm",
            status=status,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(row)
        session.flush()
        return row.id

    live = _add("active", "workspaces/k/erased.webm")
    completing = _add("completing", "workspaces/k/erased-2.webm")
    done = _add("completed", "workspaces/k/finished.webm")
    other = _add("active", "workspaces/k/untouched.webm")
    session.commit()

    stale = datetime.now(UTC) - timedelta(seconds=120)
    for key in ("workspaces/k/erased.webm", "workspaces/k/erased-2.webm"):
        fenced, drained = meeting_upload_crud.fence_key_for_deletion_sync(
            session, key=key, stale_before=stale
        )
        assert (fenced, drained) == (1, True)  # no writer in flight here
    # A key with no session at all (every non-meeting audio object) is a no-op,
    # and trivially drained.
    assert meeting_upload_crud.fence_key_for_deletion_sync(
        session, key="voiceprints/whatever.wav", stale_before=stale
    ) == (0, True)

    statuses = {
        row.id: row.status
        for row in session.execute(select(MeetingUploadSession)).scalars()
    }
    assert statuses[live] == "expiring"
    assert statuses[completing] == "expiring"
    assert statuses[done] == "completed"  # terminal rows are not disturbed
    assert statuses[other] == "active"  # a different key is untouched
    session.close()


def test_deletion_refuses_to_erase_under_a_writer_that_never_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain has a deadline, and missing it RAISES (issue #37 r17b).

    A part write admitted before the fence can outlive every abort/verify
    round. Erasure waits for it — but a writer that never settles must not
    block the sweep forever OR be erased around: the deletion fails, retries
    with backoff, and ends up visibly `delete_failed` with the key preserved.
    """
    from app.crud import meeting_upload as meeting_upload_crud

    calls: list[str] = []

    def _never_drains(session, *, key, stale_before):
        calls.append(key)
        return (0, False)

    monkeypatch.setattr(
        meeting_upload_crud, "fence_key_for_deletion_sync", _never_drains
    )
    monkeypatch.setattr(tasks_module, "_DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(tasks_module, "_DRAIN_POLL_SECONDS", 0.01)

    with pytest.raises(RuntimeError, match="did not drain"):
        tasks_module._drain_upload_writers(None, "workspaces/k/live.webm")
    assert calls  # it really did consult the fence


def test_deletion_proceeds_once_writers_have_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary path: the fence reports drained and erasure continues."""
    from app.crud import meeting_upload as meeting_upload_crud

    rounds = {"n": 0}

    def _drains_on_second_look(session, *, key, stale_before):
        rounds["n"] += 1
        return (1, rounds["n"] > 1)

    monkeypatch.setattr(
        meeting_upload_crud, "fence_key_for_deletion_sync", _drains_on_second_look
    )
    monkeypatch.setattr(tasks_module, "_DRAIN_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(tasks_module, "_DRAIN_POLL_SECONDS", 0.01)

    tasks_module._drain_upload_writers(None, "workspaces/k/settles.webm")
    assert rounds["n"] == 2  # it WAITED, rather than erasing on the first look


def test_expiry_defers_a_session_whose_admitted_write_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL expiry must not destroy storage under an admitted writer (r18).

    The claim stops NEW admissions, but a part write admitted before it is
    still streaming — and can land after the abort's final empty listing.
    Expiring the session (whose CASCADE takes the recording) would orphan
    those parts with nothing owning the key. The sweep defers the session,
    keeping its claim; once the writer settles (or its process dies and the
    counter ages out) the next sweep finishes the job.
    """
    from datetime import timedelta

    from sqlalchemy import create_engine, event, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_upload_session import MeetingUploadSession
    from app.models.workspace import Workspace
    from app.worker.tasks import expire_upload_sessions

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = Session(engine)
    ws = Workspace(name="w")
    session.add(ws)
    session.flush()
    rec = MeetingRecording(workspace_id=ws.id, title="writing", status="uploading")
    session.add(rec)
    session.flush()
    upload = MeetingUploadSession(
        recording_id=rec.id,
        workspace_id=ws.id,
        key="workspaces/k/writing.webm",
        s3_upload_id="mp-writing",
        part_size_bytes=8,
        filename="w.webm",
        content_type="audio/webm",
        status="active",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        inflight_writes=1,
        inflight_since=datetime.now(UTC),  # a LIVE, heartbeating writer
    )
    session.add(upload)
    session.commit()
    upload_id = upload.id

    aborts: list[str] = []
    store = SimpleNamespace(
        iter_multipart_uploads=lambda *, prefix="": iter([]),
        abort_multipart=lambda key, uid: (aborts.append(uid), True)[1],
        list_multipart_parts=lambda key, uid: [],
        head=lambda key: (_ for _ in ()).throw(KeyError(key)),
    )
    monkeypatch.setattr(tasks_module.settings, "AUDIO_S3_ENDPOINT_URL", "http://s4")
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: store)
    monkeypatch.setattr(tasks_module, "_SWEEP_DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(tasks_module, "_DRAIN_POLL_SECONDS", 0.01)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    # Round 1: the writer is live — nothing is destroyed, the claim is kept.
    assert expire_upload_sessions() == {"expired": 0}
    assert aborts == []
    row = session.execute(
        select(MeetingUploadSession).where(MeetingUploadSession.id == upload_id)
    ).scalar_one()
    session.refresh(row)
    assert row.status == "expiring"  # claimed, deferred — not abandoned

    # The writer finishes and releases its admission; the claim goes stale.
    row.inflight_writes = 0
    session.commit()
    from app.api.v1.routes import meetings as meetings_routes

    monkeypatch.setattr(meetings_routes, "COMPLETION_LEASE_SECONDS", 0)

    assert expire_upload_sessions() == {"expired": 1}
    assert aborts == ["mp-writing"]
    session.close()


def test_expire_upload_sessions_yields_to_a_completion_reclaimed_mid_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep must CLAIM before it destroys storage (issue #37 review r12).

    Listing expired sessions and then aborting them is a read-then-write: in
    the gap, a client can reclaim the stale completion lease and start a
    legitimate completion. Here that reclaim is injected precisely into that
    gap — the sweep must then abort nothing and leave the recording alone.
    Without the conditional claim it aborts the multipart upload and deletes
    the recording out from under a live completion.
    """
    from datetime import timedelta

    from sqlalchemy import create_engine, event, select, update
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.crud import meeting_upload as meeting_upload_crud
    from app.db.base import Base
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_upload_session import MeetingUploadSession
    from app.models.workspace import Workspace
    from app.worker.tasks import expire_upload_sessions

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = Session(engine)

    ws = Workspace(name="w")
    session.add(ws)
    session.flush()
    rec = MeetingRecording(
        workspace_id=ws.id,
        title="live",
        status="uploading",
        audio_key="workspaces/k/live.webm",
        audio_content_type="audio/webm",
    )
    session.add(rec)
    session.flush()
    upload = MeetingUploadSession(
        recording_id=rec.id,
        workspace_id=ws.id,
        key="workspaces/k/live.webm",
        s3_upload_id="mp-live",
        part_size_bytes=8,
        filename="live.webm",
        content_type="audio/webm",
        status="completing",
        completing_at=datetime.now(UTC) - timedelta(seconds=600),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(upload)
    session.commit()
    upload_id = upload.id

    real_list = meeting_upload_crud.list_expired_sync

    def _list_then_reclaim(sess, **kwargs):
        rows = real_list(sess, **kwargs)
        # The competing client lands HERE — between the sweep's read and its
        # claim — refreshing the lease exactly as claim_for_completion does.
        sess.execute(
            update(MeetingUploadSession)
            .where(MeetingUploadSession.id == upload_id)
            .values(status="completing", completing_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        sess.commit()
        return rows

    monkeypatch.setattr(
        tasks_module.meeting_upload_crud, "list_expired_sync", _list_then_reclaim
    )

    aborts: list[str] = []
    store = SimpleNamespace(
        iter_multipart_uploads=lambda *, prefix="": iter([]),
        abort_multipart=lambda key, upload_id: (aborts.append(upload_id), True)[1],
        list_multipart_parts=lambda key, upload_id: [],
        head=lambda key: {"size": 1},
    )
    monkeypatch.setattr(tasks_module.settings, "AUDIO_S3_ENDPOINT_URL", "http://s4")
    monkeypatch.setattr("app.services.audio_store.get_audio_store", lambda: store)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    assert expire_upload_sessions() == {"expired": 0}
    assert aborts == []  # the live multipart upload was left intact
    row = session.execute(select(MeetingUploadSession)).scalars().one()
    assert row.status == "completing"  # still the client's, not `expiring`
    assert session.execute(select(MeetingRecording)).scalars().one().id == rec.id
    session.close()


def test_reconciler_resends_or_retires_keyed_outbox_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sent enrollment rows are never age-pruned (review round 5) — the
    reconciler re-sends ones whose sample is still waiting and deletes ones
    whose work verifiably landed, so protection has no expiry clock."""
    from datetime import timedelta

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.audio_object import AudioObject
    from app.models.task_outbox import TaskOutbox
    from app.worker.tasks import reconcile_transcription_attempts

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    old = datetime.now(UTC) - timedelta(hours=2)
    session.add_all(
        [
            # Sample still waiting (active, unstamped) ⇒ re-send.
            AudioObject(bucket="b", key="waiting", kind="enrollment_sample"),
            TaskOutbox(
                task_name="tasks.enroll_voiceprint",
                queue="transcription",
                payload={"sample_key": "waiting"},
                dedupe_key="enroll:waiting",
                status="sent",
                sent_at=old,
            ),
            # Sample stamped (work landed, worker crashed pre-consume) ⇒ delete.
            AudioObject(
                bucket="b", key="done", kind="enrollment_sample", ref_id=uuid.uuid4()
            ),
            TaskOutbox(
                task_name="tasks.enroll_voiceprint",
                queue="transcription",
                payload={"sample_key": "done"},
                dedupe_key="enroll:done",
                status="sent",
                sent_at=old,
            ),
            # Fresh sent row (inside the resend window) ⇒ untouched.
            AudioObject(bucket="b", key="fresh", kind="enrollment_sample"),
            TaskOutbox(
                task_name="tasks.enroll_voiceprint",
                queue="transcription",
                payload={"sample_key": "fresh"},
                dedupe_key="enroll:fresh",
                status="sent",
                sent_at=datetime.now(UTC),
            ),
        ]
    )
    session.commit()

    monkeypatch.setattr(tasks_module.settings, "MEETINGS_ENABLED", True)

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(tasks_module, "session_scope", _scope)

    reconcile_transcription_attempts()
    session.expire_all()

    rows = {
        r.dedupe_key: r for r in session.execute(select(TaskOutbox)).scalars().all()
    }
    assert rows["enroll:waiting"].status == "pending"  # dispatcher will re-send
    assert rows["enroll:waiting"].next_attempt_at is not None
    assert "enroll:done" not in rows  # finished business — marker retired
    assert rows["enroll:fresh"].status == "sent"
    session.close()


@pytest.mark.parametrize("requested_language", ["auto", "en", "sv"])
@pytest.mark.parametrize("skip_full_pass", [False, True])
def test_requested_language_reaches_every_transcription_request(
    monkeypatch, requested_language, skip_full_pass
):
    calls = _wire_pipeline(
        monkeypatch,
        stt_result=SimpleNamespace(
            words=[],
            text="",
            language="sv",
            duration=70.0,
            has_word_timestamps=False,
        ),
        turns=[SpeakerTurn("SPEAKER_00", 0.0, 70.0)],
        requested_language=requested_language,
    )
    if skip_full_pass:
        monkeypatch.setattr(tasks_module.settings, "STT_MAX_UPLOAD_MB", -1)
    result = transcribe_attempt(str(uuid.uuid4()))
    assert result["status"] == "completed"
    # Skipped: three 30 s pieces of whole-file text plus three turn chunks.
    expected_calls = 6 if skip_full_pass else 4
    assert len(calls["stt_calls"]) == expected_calls
    # Even if a server reports a different language, the saved user choice wins
    # for every later speaker turn / 30-second chunk.
    expected = None if requested_language == "auto" else requested_language
    assert calls["stt_languages"] == [expected] * expected_calls
