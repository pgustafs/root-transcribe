"""Celery transcription, speaker enrollment, audio lifecycle and governance tasks.

Tasks use synchronous database sessions, durable outbox delivery and retry claims."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from app.core.config import settings
from app.core.logging import log_operation
from app.core.metrics import (
    MEETING_ATTEMPTS,
    MEETING_CLIENT_EVENTS,
    MEETING_DELETION_BACKLOG,
    MEETING_DELETIONS,
    MEETING_MEDIA_REJECTED,
    MEETING_OUTBOX_DEAD,
    MEETING_OUTBOX_PENDING,
    MEETING_QUEUE_WAIT_SECONDS,
    MEETING_REALTIME_FACTOR,
    MEETING_SPEAKER_MATCHES,
    MEETING_STEP_SECONDS,
    MEETING_UPLOADS,
)
from app.crud import audio_object as audio_object_crud
from app.crud import background_job as background_job_crud
from app.crud import meeting_recording as meeting_recording_crud
from app.crud import meeting_upload as meeting_upload_crud
from app.crud import speaker_consent as speaker_consent_crud
from app.crud import speaker_profile as speaker_profile_crud
from app.crud import task_outbox as task_outbox_crud
from app.crud import transcript_revision as transcript_revision_crud
from app.crud import transcription_attempt as transcription_attempt_crud
from app.crud import user as user_crud  # noqa: F401  (kept for parity/examples)
from app.db.sync_session import session_scope
from app.models.audio_object import AudioObject
from app.models.task_outbox import TaskOutbox
from app.services import retention, speaker_matching
from app.services.workspace_activity import record_activity_sync
from app.worker.celery_app import celery_app

logger = logging.getLogger("app.worker.tasks")

#: Outbox tasks whose payloads reference audio-store keys. Their sent rows are
#: liveness markers protecting external state (the orphan-intent sweep skips
#: any key they reference), so they are exempt from age pruning and instead
#: retired by worker consumption or the reconciler (review rounds 4–5).
_KEYED_OUTBOX_TASKS = ("tasks.enroll_voiceprint",)


@celery_app.task(name="tasks.cleanup_old_jobs")
def cleanup_old_jobs(max_age_days: int | None = None) -> dict[str, int]:
    """Prune the background-job ledger of terminal rows past the retention window.

    Mirrors Celery's ``result_expires`` for our durable job records. ``max_age_days``
    defaults to ``settings.JOBS_RETENTION_DAYS``. Schedule it periodically (Celery
    beat / cron). Only terminal (success/failure/revoked) rows are removed.
    """
    days = max_age_days if max_age_days is not None else settings.JOBS_RETENTION_DAYS
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with session_scope() as session:
        deleted = background_job_crud.delete_terminal_older_than(session, cutoff)
        # Sent outbox rows are NOT pure history (review round 4): the
        # orphan-intent sweep reads them as proof that an unstamped audio key
        # still has queued work. Key-bearing tasks are excluded from age
        # pruning entirely (review round 5: any clock re-opens the data-loss
        # window) — their rows are removed only by worker consumption or the
        # reconciler once the work is verifiably done or the sample is gone.
        task_outbox_crud.prune_sent_sync(
            session,
            older_than=datetime.now(UTC)
            - timedelta(hours=settings.OUTBOX_SENT_RETENTION_HOURS),
            exclude_task_names=_KEYED_OUTBOX_TASKS,
        )
        session.commit()
    logger.info(
        "pruned %d old background-job record(s)",
        deleted,
        extra={"operation": "jobs.cleanup", "deleted": deleted, "max_age_days": days},
    )
    return {"deleted": deleted}


# --------------------------------------------------------------------------
# Transactional task outbox — dispatcher + attempt reconciler (issue #6)
# --------------------------------------------------------------------------


def _deliver_outbox_row(row) -> None:
    """Send one outbox row to the broker (module-level so tests can stub it)."""
    from app.services.task_outbox import deliver_outbox_row

    deliver_outbox_row(row)


@celery_app.task(name="tasks.dispatch_task_outbox")
def dispatch_task_outbox(limit: int = 100) -> dict[str, int]:
    """Deliver pending outbox rows to the broker (the durable enqueue path).

    The fast path in the API usually beats this task; it exists for the cases
    that matter — a send that failed after commit, a broker outage, an API
    crash between commit and send. Failed sends back off exponentially and
    dead-letter after ``OUTBOX_MAX_SEND_ATTEMPTS``.
    """
    counts = {"sent": 0, "failed": 0, "dead": 0}
    now = datetime.now(UTC)
    with session_scope() as session:
        for row in task_outbox_crud.claim_due_sync(session, now=now, limit=limit):
            try:
                _deliver_outbox_row(row)
            except Exception as exc:  # noqa: BLE001 — recorded, retried, bounded
                dead = task_outbox_crud.record_failure_sync(
                    session,
                    row,
                    error=f"{type(exc).__name__}: {exc}",
                    now=datetime.now(UTC),
                    max_attempts=settings.OUTBOX_MAX_SEND_ATTEMPTS,
                    base_backoff_seconds=settings.OUTBOX_RETRY_BASE_SECONDS,
                    # Keyed rows protect external state — never dead-letter
                    # them; they retry forever at the max backoff (round 6).
                    dead_letter=row.task_name not in _KEYED_OUTBOX_TASKS,
                )
                counts["failed"] += 1
                if dead:
                    counts["dead"] += 1
                    MEETING_OUTBOX_DEAD.inc()
                    logger.error(
                        "outbox row dead-lettered",
                        extra={
                            "operation": "outbox.dead_letter",
                            "task": row.task_name,
                            "outbox_id": str(row.id),
                        },
                    )
                continue
            task_outbox_crud.mark_sent_sync(session, row, now=datetime.now(UTC))
            counts["sent"] += 1
        session.commit()
        pending = session.execute(
            select(func.count())
            .select_from(TaskOutbox)
            .where(TaskOutbox.status == "pending")
        ).scalar()
        MEETING_OUTBOX_PENDING.set(int(pending or 0))
    if counts["sent"] or counts["failed"]:
        logger.info(
            "outbox dispatch: %(sent)d sent, %(failed)d failed",
            counts,
            extra={"operation": "outbox.dispatch", **counts},
        )
    return counts


@celery_app.task(name="tasks.reconcile_transcription_attempts")
def reconcile_transcription_attempts() -> dict[str, int]:
    """Recover transcription attempts the broker lost or a worker abandoned.

    Redis is not treated as a durable record of queued work: a ``pending``
    attempt past the start timeout gets a fresh outbox row (unique per resend
    generation, bounded by ``MEETING_ATTEMPT_MAX_ENQUEUES``), and a ``running``
    attempt whose heartbeat went silent is failed so the recording becomes
    user-retryable. Aged ``pending`` recordings with no live attempt (pre-#6
    rows) are failed as a backstop.
    """
    if not settings.MEETINGS_ENABLED:
        return {"resent": 0, "failed": 0}

    resent = 0
    failed = 0
    now = datetime.now(UTC)
    with session_scope() as session:
        for attempt in transcription_attempt_crud.list_stuck_pending_sync(
            session,
            now=now,
            older_than_seconds=settings.MEETING_ATTEMPT_START_TIMEOUT_SECONDS,
        ):
            if attempt.enqueue_count >= settings.MEETING_ATTEMPT_MAX_ENQUEUES:
                attempt.status = "failed"
                attempt.error = "Transcription could not be delivered to a worker."
                attempt.finished_at = now
                meeting_recording_crud.finish_sync(
                    session,
                    attempt.recording_id,
                    status="failed",
                    error="Transcription could not be delivered to a worker; retry.",
                )
                failed += 1
                continue
            attempt.enqueue_count += 1
            task_outbox_crud.enqueue_sync(
                session,
                task_name="tasks.transcribe_attempt",
                queue="transcription",
                payload={"attempt_id": str(attempt.id)},
                dedupe_key=f"attempt:{attempt.id}:send:{attempt.enqueue_count}",
            )
            resent += 1
            logger.warning(
                "re-sending lost transcription attempt",
                extra={
                    "operation": "meeting.attempt_resend",
                    "attempt_id": str(attempt.id),
                    "enqueue_count": attempt.enqueue_count,
                },
            )

        for attempt in transcription_attempt_crud.list_stale_running_sync(
            session, now=now, stale_after_seconds=settings.MEETING_ATTEMPT_STALE_SECONDS
        ):
            attempt.status = "failed"
            attempt.error = "The transcription worker stopped responding."
            attempt.finished_at = now
            meeting_recording_crud.finish_sync(
                session,
                attempt.recording_id,
                status="failed",
                error="The transcription worker stopped responding; retry the "
                "recording.",
            )
            failed += 1

        for rec in transcription_attempt_crud.list_orphaned_pending_recordings_sync(
            session,
            now=now,
            older_than_seconds=settings.MEETING_ATTEMPT_START_TIMEOUT_SECONDS,
        ):
            meeting_recording_crud.finish_sync(
                session,
                rec.id,
                status="failed",
                error="Transcription was never started; retry the recording.",
            )
            failed += 1

        # Key-bearing outbox rows (enrollment) are never age-pruned (review
        # round 5) — this is what retires them instead. A sent row whose
        # sample is still waiting (active, unstamped) after the resend window
        # means the broker lost or starved the message: flip it back to
        # pending so the dispatcher re-sends (the worker's ref_id CAS makes a
        # late duplicate a no-op). A row whose sample is stamped or tombstoned
        # is finished business — delete it.
        keyed_cutoff = now - timedelta(seconds=settings.OUTBOX_KEYED_RESEND_SECONDS)
        keyed_rows = (
            session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.status == "sent",
                    TaskOutbox.task_name.in_(_KEYED_OUTBOX_TASKS),
                    TaskOutbox.sent_at < keyed_cutoff,
                )
            )
            .scalars()
            .all()
        )
        for row in keyed_rows:
            sample_key = (row.payload or {}).get("sample_key") or (
                row.payload or {}
            ).get("key")
            obj = (
                audio_object_crud.get_by_key_sync(session, sample_key)
                if sample_key
                else None
            )
            if obj is not None and obj.ref_id is None and obj.status == "active":
                row.status = "pending"
                row.next_attempt_at = now
                resent += 1
                logger.warning(
                    "re-sending lost keyed outbox task",
                    extra={
                        "operation": "meeting.keyed_outbox_resend",
                        "task_name": row.task_name,
                        "key": sample_key,
                    },
                )
            else:
                session.delete(row)
        session.commit()

    if resent or failed:
        logger.info(
            "attempt reconciliation: %d resent, %d failed",
            resent,
            failed,
            extra={
                "operation": "meeting.attempt_reconcile",
                "resent": resent,
                "failed": failed,
            },
        )
    # Domain jobs beyond transcription (clips and DSAR bundles) also CAS
    # pending→running before long work — a killed worker previously left them
    # "running" forever (review finding). Fail them visibly once updated_at
    # goes stale; the user re-triggers.
    from app.models.compliance_export import ComplianceExportJob
    from app.models.meeting_clip import MeetingClip

    stale_cutoff_ts = datetime.now(UTC) - timedelta(
        seconds=settings.MEETING_ATTEMPT_STALE_SECONDS
    )
    stale_jobs = 0
    with session_scope() as session:
        from app.models.meeting_dlp import DlpFinding as _DlpFinding

        stuck_findings = (
            session.execute(
                select(_DlpFinding).where(_DlpFinding.status == "redacting")
            )
            .scalars()
            .all()
        )
        for finding in stuck_findings:
            updated = finding.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated < stale_cutoff_ts:
                finding.status = "accepted"  # crashed redaction: release claim
                stale_jobs += 1
        for model, running_states in (
            (MeetingClip, ("processing",)),
            (ComplianceExportJob, ("running",)),
        ):
            rows = (
                session.execute(select(model).where(model.status.in_(running_states)))
                .scalars()
                .all()
            )
            for row in rows:
                updated = row.updated_at
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                if updated < stale_cutoff_ts:
                    row.status = "failed"
                    row.error = "worker lost mid-run; please retry"
                    stale_jobs += 1
        if stale_jobs:
            session.commit()
    return {"resent": resent, "failed": failed, "stale_domain_jobs": stale_jobs}


# --------------------------------------------------------------------------
# Meeting transcription: STT + diarization + speaker identification
# --------------------------------------------------------------------------


def _build_speaker_centroids(
    session, *, model_id: str | None = None, workspace_id=None
) -> dict[uuid.UUID, list[float]]:
    """Decrypt matching-eligible voiceprints into per-profile centroids (#6).

    Candidates are filtered HARD before any comparison:

    - the profile's latest ``speaker_identification`` consent must be a live
      grant (withdrawn/expired consent excludes it immediately);
    - the print's embedding contract (``model_id``) must exactly match the
      active embedding model — cross-model cosine scores are meaningless and
      were silently averaged in before #6;
    - a print that can't be decrypted (rotated key / corruption) is skipped
      rather than aborting identification for everyone else.
    """
    from app.crud import speaker_consent as speaker_consent_crud
    from app.services import speaker_directory

    consented = speaker_consent_crud.consented_profile_ids_sync(
        session, purpose="speaker_identification", workspace_id=workspace_id
    )
    grouped: dict[uuid.UUID, list[list[float]]] = {}
    for vp in speaker_profile_crud.list_active_voiceprints_sync(session):
        if vp.speaker_profile_id not in consented:
            continue
        if model_id is not None and vp.model_id != model_id:
            logger.info(
                "skipping model-incompatible voiceprint",
                extra={
                    "operation": "speaker.contract_mismatch",
                    "voiceprint_id": str(vp.id),
                    "print_model": vp.model_id,
                    "active_model": model_id,
                },
            )
            continue
        try:
            vec = speaker_directory.decrypt_embedding(
                vp.embedding, vp.dims, key_id=vp.key_id
            )
        except ValueError:
            logger.warning("skipping undecryptable voiceprint %s", vp.id)
            continue
        grouped.setdefault(vp.speaker_profile_id, []).append(vec)
    return {pid: speaker_directory.centroid(vecs) for pid, vecs in grouped.items()}


def _full_name_sync(session, user_id: uuid.UUID | None) -> str | None:
    """A person's name as they wrote it, or None when they gave none."""
    from app.models.user import User

    user = session.get(User, user_id) if user_id else None
    return ((user.full_name or "").strip() or None) if user is not None else None


@celery_app.task(
    name="tasks.transcribe_attempt",
    soft_time_limit=settings.STT_TIMEOUT_SECONDS,
    time_limit=settings.STT_TIMEOUT_SECONDS + 300,
)
def transcribe_attempt(attempt_id: str) -> dict[str, str]:
    """Run one idempotent transcription attempt (issue #6).

    The attempt row is the execution's identity: the worker claims it with an
    atomic ``pending → running`` CAS, so a duplicate delivery (acks_late
    redelivery, reconciler resend) returns immediately instead of re-running
    the pipeline. Heartbeats between steps let the reconciler detect a dead
    worker. On failure the user-facing state is persisted first, then the
    exception is **re-raised** so Celery records a real FAILURE.

    Pipeline (each step updates the recording's ``status``/``current_step``):

    1. **diarize** — pyannote speaker turns (local, stt-worker only).
    2. **transcribe** — remote STT with word timestamps when available.
    3. Align words to turns, or transcribe each turn when timestamps
       are unavailable or the recording exceeds the upload limit.
    4. **identify** — embed each diarized speaker and match against the enrolled
       voiceprint directory (cosine ≥ threshold ⇒ a named, human-confirmable match).
    5. persist the transcript segments and mark the recording ``completed``.
    """
    task_started = time.monotonic()
    from app.services import channel_audio, media_validation
    from app.services import turn_transcription as turn_stt
    from app.services.audio_store import get_audio_store
    from app.services.diarization import (
        decode_file_to_channel_wavs,
        decode_file_to_wav,
        get_diarization_service,
        get_embedding_service,
        load_waveform,
        probe_wav,
        save_wav,
        slice_wav,
    )
    from app.services.transcript_align import Segment as AlignedSegment
    from app.services.transcript_align import SpeakerTurn, align
    from app.services.transcription import (
        WholeFileRefused,
        get_transcription_service,
    )

    aid = uuid.UUID(attempt_id)
    with session_scope() as session:
        attempt = transcription_attempt_crud.get_sync(session, aid)
        if attempt is None:
            logger.warning("transcription attempt %s vanished", attempt_id)
            return {"status": "missing", "attempt_id": attempt_id}
        rid = attempt.recording_id
        rec = meeting_recording_crud.get_sync(session, rid)
        if rec is None or not rec.audio_key:
            transcription_attempt_crud.finish_sync(
                session, aid, status="cancelled", error="recording or audio gone"
            )
            return {"status": "cancelled", "attempt_id": attempt_id}
        recording_id = str(rid)
        workspace_id = str(rec.workspace_id)
        audio_key = rec.audio_key
        requested_language = (
            None if rec.requested_language == "auto" else rec.requested_language
        )
        expected_speakers = rec.expected_speakers
        audio_layout = rec.audio_layout
        mic_speakers = rec.mic_speakers
        # The person recording, for their own lines when the microphone of an
        # online meeting is theirs alone.
        owner_name = (
            _full_name_sync(session, rec.created_by_id)
            if audio_layout == "mic_tab" and mic_speakers == 1
            else None
        )
        # Safe operator diagnostics for a failure (issue #37): media type + byte
        # size — never audio bytes, titles, or object keys.
        size_bytes = rec.size_bytes
        content_type = rec.audio_content_type
        # Privacy-safe recorder context the client supplied at upload time
        # (issue #37 r7) — attached to a media rejection so operators can
        # correlate it with a browser WITHOUT trusting a client-sent verdict.
        client_diagnostics = rec.client_diagnostics or {}
        filename = (attempt.config or {}).get("filename") or "recording.webm"

    with session_scope() as session:
        if not transcription_attempt_crud.mark_running(session, aid):
            # Already claimed / terminal: a duplicate delivery no-ops.
            MEETING_ATTEMPTS.labels(status="duplicate").inc()
            logger.info(
                "duplicate transcription delivery ignored",
                extra={
                    "operation": "meeting.duplicate_delivery",
                    "attempt_id": attempt_id,
                },
            )
            return {"status": "duplicate", "attempt_id": attempt_id}
    queued_at = attempt.created_at
    if queued_at is not None:
        if queued_at.tzinfo is None:
            queued_at = queued_at.replace(tzinfo=UTC)
        MEETING_QUEUE_WAIT_SECONDS.observe(
            max(0.0, (datetime.now(UTC) - queued_at).total_seconds())
        )

    step_clock: dict[str, object] = {"name": None, "started": 0.0}

    def _step(status_value: str, step: str) -> None:
        prev_name = step_clock["name"]
        if prev_name is not None:
            MEETING_STEP_SECONDS.labels(step=prev_name).observe(
                time.monotonic() - step_clock["started"]  # type: ignore[operator]
            )
        step_clock["name"] = step
        step_clock["started"] = time.monotonic()
        with session_scope() as session:
            meeting_recording_crud.mark_status_sync(
                session, rid, status=status_value, step=step
            )
            transcription_attempt_crud.heartbeat_sync(session, aid, step=step)

    wav_path: str | None = None
    # An online meeting's two sources, each as its own WAV (microphone, tab).
    channel_paths: tuple[str, str] | None = None
    status = "completed"
    error: str | None = None
    error_code: str | None = None
    segment_count = 0
    started = time.monotonic()
    try:
        # Download + decode are preparation for diarization, which runs first
        # (its turns anchor all attribution). File-based end to end (issue #6):
        # the compressed object streams to disk, ffmpeg decodes file→file, and
        # NOTHING holds whole-recording bytes in Python memory.
        _step("diarizing", "decode")
        suffix = "." + (filename.rsplit(".", 1)[-1] if "." in filename else "webm")
        ifd, in_path = tempfile.mkstemp(suffix=suffix)
        os.close(ifd)
        # Disk budget (review finding — the old heuristic under-estimated):
        # the compressed input (worst case AUDIO_MAX_BYTES) and the decoded
        # 16 kHz mono 16-bit WAV (bounded by the duration cap thanks to the
        # ffmpeg -t bound below) must BOTH fit, plus 25% headroom.
        max_seconds = settings.MEETING_MAX_DURATION_MINUTES * 60
        free = shutil.disk_usage(tempfile.gettempdir()).free
        pcm_bytes = int((max_seconds + 1) * 16000 * 2)
        if audio_layout == "mic_tab":
            pcm_bytes *= 3  # the mix, and each channel on its own
        needed = int((int(settings.AUDIO_MAX_BYTES) + pcm_bytes) * 1.25)
        if free < needed:
            raise RuntimeError(
                "Not enough temporary disk space to process this recording"
            )
        get_audio_store().download_to_path(audio_key, in_path)
        # Media-validation boundary (issue #37): a malformed/incomplete container
        # is a NON-retryable media fault, classified with a stable, safe code —
        # NOT a transient STT outage, and never surfaced as raw ffmpeg stderr.
        # This runs BEFORE any model loads; the bounded decode is authoritative
        # (declared filename/MIME/size are not trusted).
        try:
            media_validation.precheck_media_file(in_path)
            # decode_file_to_wav classifies + sanitizes a decode failure into a
            # MediaValidationError itself (issue #37 review — so the raw ffmpeg
            # command/paths never reach the operator log via log_operation).
            wav_path = decode_file_to_wav(
                in_path, src_hint=filename, max_seconds=max_seconds
            )
            if audio_layout == "mic_tab":
                channel_paths = decode_file_to_channel_wavs(
                    in_path, max_seconds=max_seconds
                )
        finally:
            with contextlib.suppress(OSError):
                os.remove(in_path)
        # Header-only probe BEFORE anything loads audio into memory: a tiny
        # compressed file that decodes to hours of PCM is rejected here.
        wav_duration, _sample_rate = probe_wav(wav_path)
        max_seconds = settings.MEETING_MAX_DURATION_MINUTES * 60
        if wav_duration <= 0:
            # ffmpeg "succeeded" but produced no audio — e.g. a container with no
            # decodable audio stream. Non-retryable: the object won't change.
            raise media_validation.MediaValidationError(
                media_validation.MediaErrorCode.INCOMPLETE,
                detail="decoded output has zero audio frames",
            )
        if wav_duration > max_seconds:
            raise media_validation.MediaValidationError(
                media_validation.MediaErrorCode.LIMIT_EXCEEDED,
                detail=(
                    f"decoded duration {wav_duration / 60:.0f} min exceeds the "
                    f"{settings.MEETING_MAX_DURATION_MINUTES} min cap"
                ),
            )
        service = get_transcription_service()
        language: str | None = None
        duration: float | None = wav_duration
        # Whether the server takes a whole track in one request. Learned from
        # its answer, and remembered for the next track of the same recording.
        whole_file_accepted = True

        def _detect(audio: dict, count: int | None) -> list[SpeakerTurn]:
            """Who speaks when. Audio known to hold one person needs no telling
            voices apart, only where there is speech, which is far faster."""
            detector = get_diarization_service()
            if count == 1:
                return detector.speech_sync(audio)
            return detector.diarize_sync(audio, num_speakers=count)

        def _transcribe_track(
            wav_path: str, turns: list[SpeakerTurn]
        ) -> list[AlignedSegment]:
            """Speech-to-text for one track (the recording, or one channel of
            an online meeting): the turns' words, under the turns' speakers."""
            nonlocal language, duration, whole_file_accepted
            aligned = None
            # The whole recording as plain text, when the server gave it: the
            # reference for what was said if turns are transcribed one by one.
            full_text = ""
            # The STT server caps upload size (vLLM: 25 MB ⇒ ~13 min of 16 kHz WAV;
            # a 60-min meeting is ~115 MB), so the full-file pass only happens for
            # files under the cap — longer meetings go straight to per-turn slices,
            # each of which stays tiny.
            max_upload = settings.STT_MAX_UPLOAD_MB * 1024 * 1024
            wav_size = os.path.getsize(wav_path)
            stt = None
            if wav_size <= max_upload and whole_file_accepted:
                _step("transcribing", "transcribe")
                try:
                    stt = service.transcribe_file_sync(
                        wav_path, "audio.wav", language=requested_language
                    )
                except WholeFileRefused as exc:
                    # Under our own size cap, and still refused: servers have
                    # limits of their own (vLLM: 600 s of audio per request).
                    # The whole-file text comes from pieces instead. A server
                    # that is failing altogether refuses the first piece too.
                    whole_file_accepted = False
                    logger.warning(
                        "STT server refused the whole file — text in pieces",
                        extra={
                            "operation": "meeting.stt_full_pass_refused",
                            "status": exc.status,
                            "reason": str(exc)[:300],
                            "seconds": round(wav_duration, 1),
                            "recording_id": recording_id,
                        },
                    )
            if stt is not None:
                language = stt.language
                duration = stt.duration or wav_duration
                full_text = str(getattr(stt, "text", "") or "")
                if stt.has_word_timestamps:
                    # Word timestamps present: one-pass whisperX-style alignment.
                    aligned = align(stt.words, turns)
            else:
                # Too large for one request: the whole-file text is still needed
                # (it is the reference that completes the turns below), so it is
                # transcribed in pieces of at most 30 s, cut only in pauses.
                _step("transcribing", "transcribe")
                windows = turn_stt.window_bounds(turns, wav_duration)
                logger.info(
                    "whole-file text in pieces",
                    extra={
                        "operation": "meeting.stt_full_pass_windowed",
                        "bytes": wav_size,
                        "max_upload_mb": settings.STT_MAX_UPLOAD_MB,
                        "windows": len(windows),
                        "recording_id": recording_id,
                    },
                )
                pieces: list[str] = []
                for start, end in windows:
                    with session_scope() as session:
                        transcription_attempt_crud.heartbeat_sync(
                            session, aid, step="transcribe"
                        )
                    piece = service.transcribe_sync(
                        slice_wav(wav_path, start, end, turn_stt.SILENCE_SECONDS),
                        "window.wav",
                        language=requested_language,
                    )
                    if language is None:
                        language = piece.language
                    if piece.text.strip():
                        pieces.append(piece.text.strip())
                full_text = " ".join(pieces)

            if aligned is None:
                # Per-turn transcription: no word timestamps (vLLM whisper — its
                # segment times degrade to ~30s chunk boundaries on long audio,
                # drifting turn edges and bleeding text across speakers) or the file
                # was too large for a full pass. The diarization turns ARE accurate,
                # so transcribe each turn's audio slice individually — attribution
                # is exact by construction and every timestamp comes from pyannote.
                # Turns longer than 30s are sub-chunked (whisper's native window;
                # also keeps every request far below the upload cap).
                logger.info(
                    "using per-turn transcription",
                    extra={
                        "operation": "meeting.per_turn_stt",
                        "turns": len(turns),
                        "recording_id": recording_id,
                    },
                )
                _step("transcribing", "transcribe-turns")
                chunk_s = 30.0
                # One speaker's turns with almost no pause between them are one
                # stretch of speech: a sentence is not split across two requests.
                ordered = turn_stt.join_close_turns(turns)
                texts: list[str] = []
                for index, turn in enumerate(ordered):
                    if turn.end - turn.start < turn_stt.MIN_TURN_SECONDS:
                        texts.append("")  # too short to carry speech
                        continue
                    # A little real audio around the turn (never into a
                    # neighbour's speech): a cut exactly at the edges clips the
                    # first and last sound, and the word with it.
                    clip_start, clip_end = turn_stt.clip_bounds(
                        ordered, index, wav_duration
                    )
                    parts: list[str] = []
                    cursor = turn.start
                    while cursor < turn.end:
                        # Heartbeat per 30 s STT chunk (review round 2: a single
                        # long monologue turn spans MANY chunks, so a per-turn
                        # beat could still go quiet for most of the stale window).
                        with session_scope() as session:
                            transcription_attempt_crud.heartbeat_sync(
                                session, aid, step="transcribe-turns"
                            )
                        upper = min(cursor + chunk_s, turn.end)
                        clip = slice_wav(
                            wav_path,
                            clip_start if cursor == turn.start else cursor,
                            clip_end if upper == turn.end else upper,
                            turn_stt.SILENCE_SECONDS,
                        )
                        piece = service.transcribe_sync(
                            clip, "turn.wav", language=requested_language
                        )
                        if language is None:
                            language = piece.language
                        if piece.text.strip():
                            parts.append(piece.text.strip())
                        cursor = upper
                    texts.append(" ".join(parts))

                # A short turn ("Hej!", "Hur mår du?") is often too little audio
                # for the model and comes back empty. Try it again with a few
                # seconds of context and take the neighbours' known words out.
                empty = rescued = 0
                for index, turn in enumerate(ordered):
                    length = turn.end - turn.start
                    if (
                        texts[index]
                        or length < turn_stt.MIN_TURN_SECONDS
                        or length > turn_stt.RESCUE_MAX_SECONDS
                    ):
                        continue
                    empty += 1
                    with session_scope() as session:
                        transcription_attempt_crud.heartbeat_sync(
                            session, aid, step="transcribe-turns"
                        )
                    wide_start, wide_end = turn_stt.context_bounds(turn, wav_duration)
                    heard = service.transcribe_sync(
                        slice_wav(
                            wav_path, wide_start, wide_end, turn_stt.SILENCE_SECONDS
                        ),
                        "context.wav",
                        language=requested_language,
                    ).text
                    before, after = turn_stt.context_texts(
                        ordered, texts, index, (wide_start, wide_end)
                    )
                    found = turn_stt.rescue_text(
                        heard, before, after, turn_seconds=length
                    )
                    if found:
                        texts[index] = found
                        rescued += 1
                if empty:
                    logger.info(
                        "speaker turns came back empty",
                        extra={
                            "operation": "meeting.per_turn_empty",
                            "empty": empty,
                            "rescued": rescued,
                            "recording_id": recording_id,
                        },
                    )
                # One stretch of a speaker's speech cut into pieces: heard again
                # as a whole and shown as one segment (never losing a word).
                runs = turn_stt.piece_runs(ordered, texts)
                if runs:
                    first_of = {run[0]: run for run in runs}
                    merged = {index for run in runs for index in run}
                    new_turns: list[SpeakerTurn] = []
                    new_texts: list[str] = []
                    for index, turn in enumerate(ordered):
                        run = first_of.get(index)
                        if run is None:
                            if index not in merged:
                                new_turns.append(turn)
                                new_texts.append(texts[index])
                            continue
                        whole = SpeakerTurn(
                            turn.speaker, turn.start, ordered[run[-1]].end
                        )
                        with session_scope() as session:
                            transcription_attempt_crud.heartbeat_sync(
                                session, aid, step="transcribe-turns"
                            )
                        clip_start, _ = turn_stt.clip_bounds(
                            ordered, run[0], wav_duration
                        )
                        _, clip_end = turn_stt.clip_bounds(
                            ordered, run[-1], wav_duration
                        )
                        heard = service.transcribe_sync(
                            slice_wav(
                                wav_path, clip_start, clip_end, turn_stt.SILENCE_SECONDS
                            ),
                            "pieces.wav",
                            language=requested_language,
                        ).text
                        new_turns.append(whole)
                        new_texts.append(
                            turn_stt.pick_run_text(heard, [texts[i] for i in run])
                        )
                    logger.info(
                        "pieces of speech merged",
                        extra={
                            "operation": "meeting.per_turn_pieces",
                            "runs": len(runs),
                            "turns": len(merged),
                            "recording_id": recording_id,
                        },
                    )
                    ordered, texts = new_turns, new_texts
                # The model sometimes stops after the first sentence of a clip.
                # The whole-file text has those words: put them back.
                texts, put_back = turn_stt.fill_from_full_text(
                    full_text, ordered, texts
                )
                if put_back:
                    logger.info(
                        "words missing from speaker turns were put back",
                        extra={
                            "operation": "meeting.per_turn_completed",
                            "runs": put_back,
                            "recording_id": recording_id,
                        },
                    )
                # A speaker change found a second early or late leaves the end of
                # a sentence under the next speaker: give it back.
                texts, rejoined = turn_stt.realign_sentences(full_text, ordered, texts)
                if rejoined:
                    logger.info(
                        "sentences split by a speaker change were put back together",
                        extra={
                            "operation": "meeting.per_turn_realigned",
                            "sentences": rejoined,
                            "recording_id": recording_id,
                        },
                    )
                aligned = [
                    AlignedSegment(
                        speaker=turn.speaker, start=turn.start, end=turn.end, text=text
                    )
                    for turn, text in zip(ordered, texts, strict=True)
                    if text
                ]
            return aligned

        # What is said on each track, and the audio to recognise voices from.
        tracks: list[tuple[list[SpeakerTurn], dict]] = []
        # The label of the person recording, when the microphone is theirs alone.
        own_label: str | None = None
        two_sources = False
        if channel_paths is not None:
            mic_audio = load_waveform(channel_paths[0])
            tab_audio = load_waveform(channel_paths[1])
            mic_samples = mic_audio["waveform"][0].numpy()
            tab_samples = tab_audio["waveform"][0].numpy()
            two_sources = channel_audio.channels_differ(mic_samples, tab_samples)
            if not two_sources:
                # Said to be microphone + tab, but both channels hold one mix.
                logger.info(
                    "channels hold the same signal — treating the recording as mixed",
                    extra={
                        "operation": "meeting.channels_same",
                        "recording_id": recording_id,
                    },
                )
                del mic_audio, tab_audio, mic_samples, tab_samples
        if two_sources:
            # An online meeting: the microphone and the meeting tab are apart,
            # so "here" and "in the call" are known from the channel. Each is
            # detected and transcribed on its own.
            _step("diarizing", "diarize")
            mic_turns = _detect(mic_audio, mic_speakers)
            # The call heard through the loudspeakers is not someone here.
            rate = int(mic_audio["sample_rate"])
            mic_turns, echoes = channel_audio.drop_echo(
                mic_turns, mic_samples, tab_samples, rate
            )
            if echoes:
                # Silence them in the audio too (the samples are the loaded
                # waveform's own), and in the file the speech model reads:
                # otherwise the whole-file text still hears the call there.
                channel_audio.mute(mic_samples, echoes, rate)
                save_wav(channel_paths[0], mic_samples, rate)
            mic_turns, mic_labels = channel_audio.relabel(mic_turns, 0)
            tab_turns = _detect(tab_audio, expected_speakers)
            tab_turns, tab_labels = channel_audio.relabel(tab_turns, len(mic_labels))
            logger.info(
                "microphone and tab detected apart",
                extra={
                    "operation": "meeting.two_sources",
                    "mic_turns": len(mic_turns),
                    "mic_speakers": len(mic_labels),
                    "tab_turns": len(tab_turns),
                    "tab_speakers": len(tab_labels),
                    "echo_turns_dropped": len(echoes),
                    "recording_id": recording_id,
                },
            )
            if mic_speakers == 1 and mic_labels:
                own_label = "SPEAKER_00"
            # A side on which nobody spoke has nothing to transcribe.
            aligned = sorted(
                [
                    segment
                    for path, side in (
                        (channel_paths[0], mic_turns),
                        (channel_paths[1], tab_turns),
                    )
                    if side
                    for segment in _transcribe_track(path, side)
                ],
                key=lambda segment: (segment.start, segment.end),
            )
            tracks = [(mic_turns, mic_audio), (tab_turns, tab_audio)]
        else:
            # Load once as a pyannote waveform dict (avoids its torchcodec file
            # reader) — the one unavoidable in-memory representation: pyannote
            # consumes the full waveform.
            waveform = load_waveform(wav_path)
            # Diarization first — its turns are the backbone of attribution.
            _step("diarizing", "diarize")
            count = expected_speakers
            if audio_layout == "mic_tab":
                # The counts were given per side; one mix holds both.
                count = (
                    mic_speakers + expected_speakers
                    if mic_speakers and expected_speakers
                    else None
                )
            turns = _detect(waveform, count)
            aligned = _transcribe_track(wav_path, turns)
            tracks = [(turns, waveform)]

        # 4. Identify speakers against the enrolled directory (issue #6:
        # consent + exact-model candidates, several clean turns per speaker,
        # calibrated score+margin gate; uncertainty → review queue, never an
        # automatic name).
        _step("identifying", "identify")
        embed_service = get_embedding_service()
        with session_scope() as session:
            centroids = _build_speaker_centroids(
                session,
                model_id=getattr(embed_service, "model_id", None),
                workspace_id=uuid.UUID(workspace_id),
            )
        label_to_match: dict[str, tuple[uuid.UUID | None, float | None]] = {}
        pending_suggestions: list[dict] = []
        for turns, waveform in tracks if centroids else []:
            for label in sorted({t.speaker for t in turns}):
                clean_turns = speaker_matching.select_turns(
                    turns,
                    label,
                    min_seconds=settings.SPEAKER_MATCH_MIN_TURN_SEC,
                    max_turns=settings.SPEAKER_MATCH_MAX_TURNS,
                )
                if not clean_turns:
                    continue
                turn_embeddings = []
                for turn in clean_turns:
                    embedded = embed_service.embed_speakers_sync(waveform, [turn])
                    if label in embedded:
                        turn_embeddings.append(embedded[label])
                if not turn_embeddings:
                    continue
                decision = speaker_matching.decide(
                    speaker_matching.score_profiles(turn_embeddings, centroids),
                    threshold=settings.SPEAKER_MATCH_THRESHOLD,
                    margin=settings.SPEAKER_MATCH_MARGIN,
                    suggest_threshold=settings.SPEAKER_MATCH_SUGGEST_THRESHOLD,
                    sample_turns=len(turn_embeddings),
                )
                if decision.profile_id is not None:
                    MEETING_SPEAKER_MATCHES.labels(decision="auto").inc()
                    label_to_match[label] = (decision.profile_id, decision.score)
                elif decision.suggestion_profile_id is not None:
                    MEETING_SPEAKER_MATCHES.labels(decision="review").inc()
                    pending_suggestions.append(
                        {
                            "speaker_label": label,
                            "suggested_profile_id": decision.suggestion_profile_id,
                            "score": decision.score,
                            "margin": decision.margin,
                            "sample_turns": decision.sample_turns,
                        }
                    )
                else:
                    MEETING_SPEAKER_MATCHES.labels(decision="none").inc()

        # 5. Persist segments + finish.
        seg_rows = []
        for idx, seg in enumerate(aligned):
            pid, conf = label_to_match.get(seg.speaker, (None, None))
            row = {
                "idx": idx,
                "start_ms": int(seg.start * 1000),
                "end_ms": int(seg.end * 1000),
                "speaker_label": seg.speaker,
                "speaker_profile_id": pid,
                "speaker_confidence": conf,
                "text": seg.text,
            }
            if seg.speaker == own_label and pid is None and owner_name:
                # Alone at the microphone: these lines are the person
                # recording. A name like any other; it can be changed.
                row["speaker_display_name"] = owner_name
            seg_rows.append(row)
        segment_count = len(seg_rows)
        with session_scope() as session:
            # ONE transaction acknowledges the attempt AND persists its result
            # (review round 2): the completed CAS, the revision + segments,
            # the recording completion, and the DLP outbox row all commit
            # together — a crash leaves the attempt running (reclaimable),
            # never a terminal attempt with no transcript.
            won = transcription_attempt_crud.finish_sync(
                session,
                aid,
                status="completed",
                stt_model=settings.STT_MODEL,
                diarization_model=settings.DIARIZATION_MODEL,
                embedding_model_id=settings.SPEAKER_EMBEDDING_MODEL,
                commit=False,
            )
            if not won:
                # The stale-heartbeat reconciler reclaimed this attempt while
                # we were still working: a retry may already be running, so
                # this zombie must not move recording state or pointers.
                session.rollback()
                logger.warning(
                    "attempt %s was reclaimed mid-run; discarding results",
                    attempt_id,
                )
                MEETING_ATTEMPTS.labels(status="superseded").inc()
                return {"status": "superseded", "attempt_id": attempt_id}
            # Persist the NEW immutable revision (never touching prior ones),
            # then carry human edits from the previous revision forward — a
            # retry can no longer destroy corrections (issue #6).
            revision = transcript_revision_crud.create_revision_sync(
                session,
                recording_id=rid,
                attempt_id=aid,
                origin="pipeline",
                seg_rows=seg_rows,
            )
            transcript_revision_crud.carry_over_edits_sync(
                session, revision, previous_revision_id=revision.parent_revision_id
            )
            for item in pending_suggestions:
                speaker_consent_crud.add_suggestion_sync(
                    session,
                    recording_id=rid,
                    revision_id=revision.id,
                    **item,
                )
            meeting_recording_crud.finish_sync(
                session,
                rid,
                status="completed",
                duration_seconds=duration,
                language=language,
                commit=False,
            )
            if duration and duration > 0:
                MEETING_REALTIME_FACTOR.observe(
                    (time.monotonic() - task_started) / duration
                )
            if settings.MEETING_DLP_AUTO:
                # Durable enqueue (same tx as the revision): every completed
                # transcription gets a cheap regex DLP scan (issue #6, E4).
                task_outbox_crud.enqueue_sync(
                    session,
                    task_name="tasks.scan_meeting_dlp",
                    queue="celery",
                    payload={
                        "recording_id": str(rid),
                        "revision_id": str(revision.id),
                    },
                    dedupe_key=f"dlp:{revision.id}",
                )
            # Activity in the SAME transaction (review round 3): the
            # meeting.transcribed event commits with the transcript it
            # describes — a crash can't lose it, and an activity error rolls
            # the whole completion back (attempt stays reclaimable).
            # Attributed to the recording's creator (review round 4: the
            # in-tx rewrite regressed these events to "System", breaking
            # actor filtering and audit attribution).
            rec_row = meeting_recording_crud.get_sync(session, rid)
            record_activity_sync(
                session,
                workspace_id=uuid.UUID(workspace_id),
                actor_id=rec_row.created_by_id if rec_row else None,
                actor_email=rec_row.created_by_email if rec_row else None,
                event_type="meeting.transcribed",
                payload={
                    "recording_id": recording_id,
                    "title": rec_row.title if rec_row else None,
                    "segments": segment_count,
                },
            )
            session.commit()
        if step_clock["name"] is not None:
            MEETING_STEP_SECONDS.labels(step=step_clock["name"]).observe(
                time.monotonic() - step_clock["started"]  # type: ignore[operator]
            )
        MEETING_ATTEMPTS.labels(status="completed").inc()
    # SoftTimeLimitExceeded subclasses Exception, so one clause covers both a
    # pipeline error and the Celery soft-limit signal. The failure is classified
    # into a STABLE, SAFE code (issue #37): a media fault vs a retryable infra
    # error. What is persisted and shown is the safe message + code — NEVER
    # str(exc) (which leaked ffmpeg commands and temp paths). User-facing state
    # is persisted FIRST, then the exception is re-raised so Celery (and the
    # background-job ledger) record a real FAILURE instead of a green SUCCESS.
    except Exception as exc:  # noqa: BLE001 - classified + persisted, then re-raised
        media_err = media_validation.classify_pipeline_exception(exc)
        # A GENUINE media fault (precheck/decode/limit/incomplete) — not an infra
        # or internal error that classify_pipeline_exception merely wrapped.
        is_media_fault = isinstance(exc, media_validation.MediaValidationError)
        status = "failed"
        error_code = media_err.code.value
        error = media_err.message
        if not is_media_fault:
            # Unexpected internal/infra error — keep the full traceback for
            # operators; the user still sees only the safe message + code.
            logger.exception("transcription failed for recording %s", recording_id)
        with session_scope() as session:
            won = transcription_attempt_crud.finish_sync(
                session,
                aid,
                status="failed",
                error=error,
                error_code=error_code,
                commit=False,
            )
            if won:
                meeting_recording_crud.finish_sync(
                    session,
                    rid,
                    status="failed",
                    error=error,
                    error_code=error_code,
                    commit=False,
                )
                rec_row = meeting_recording_crud.get_sync(session, rid)
                record_activity_sync(
                    session,
                    workspace_id=uuid.UUID(workspace_id),
                    actor_id=rec_row.created_by_id if rec_row else None,
                    actor_email=rec_row.created_by_email if rec_row else None,
                    event_type="meeting.failed",
                    payload={
                        "recording_id": recording_id,
                        "title": rec_row.title if rec_row else None,
                        "error_code": error_code,
                    },
                )
            session.commit()
        if won:
            MEETING_ATTEMPTS.labels(status="failed").inc()
            # Count ONLY genuine media rejections, and only once the terminal CAS
            # was actually won — a superseded/lost attempt or an infra/internal
            # error must not inflate the media-rejected alarm (issue #37 review).
            if is_media_fault:
                MEETING_MEDIA_REJECTED.labels(code=error_code).inc()
                # The client-side counterpart, emitted HERE rather than by the
                # browser (issue #37 r7): only the worker knows a rejection
                # happened, so it cannot be forged by a member or replayed on
                # every reload, and it is counted exactly once — inside the
                # terminal CAS.
                MEETING_CLIENT_EVENTS.labels(event="media_rejected").inc()
                # Expected terminal outcome for bad media — a bounded, sanitized
                # operator record, no traceback, no audio/title/key. Emitted with
                # the metrics so the log and the series can never disagree.
                logger.warning(
                    "meeting recording rejected by media validation",
                    extra={
                        "operation": "meeting.media_rejected",
                        "workspace_id": workspace_id,
                        "recording_id": recording_id,
                        "attempt_id": attempt_id,
                        "error_code": error_code,
                        "retryable": media_err.retryable,
                        "stage": step_clock["name"] or "decode",
                        # NEVER the raw content_type: it is caller-supplied, so
                        # logging it verbatim would smuggle free text into the
                        # operator log (issue #37 review round 8).
                        "media_container": media_validation.normalize_container(
                            content_type
                        ),
                        "size_bytes": size_bytes,
                        "detail": media_err.detail,
                        # Closed tokens + bounded aggregates, validated on ingest.
                        **{f"client_{k}": v for k, v in client_diagnostics.items()},
                    },
                )
        raise
    finally:
        for path in (wav_path, *(channel_paths or ())):
            if path:
                with contextlib.suppress(OSError):
                    os.remove(path)
        # The one-line-per-run summary the Grafana meetings dashboard aggregates
        # (status + duration_ms + segments — see ARCHITECTURE.md).
        logger.info(
            "meeting transcription %s",
            status,
            extra={
                "operation": "meeting.transcribe",
                "workspace_id": workspace_id,
                "recording_id": recording_id,
                "attempt_id": attempt_id,
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "segments": segment_count,
            },
        )
    return {"status": status, "recording_id": recording_id, "attempt_id": attempt_id}


def _enrollment_block_reason(session, pid, obj, profile) -> str | None:
    """The SPECIFIC reason an enrollment must NOT process the raw sample, or
    None if it may proceed (issue #14 review). Distinct reasons give accurate
    deletion provenance and user guidance instead of mislabelling a deactivation
    as a consent withdrawal. ``obj`` is the sample's AudioObject; ``profile`` is
    the locked profile row.

    Fails CLOSED on a MISSING/revoked profile or lifecycle row: enrollment is
    intent-first, so the object always exists for a real sample — its absence is
    an anomaly, and a biometric path must not process on missing evidence."""
    if profile is None:
        return "profile_erased"
    if profile.status != "active":
        return "profile_deactivated"
    if obj is None or obj.status != "active":
        return "sample_unavailable"
    grant = speaker_consent_crud.current_record_sync(
        session, profile_id=pid, purpose="speaker_identification"
    )
    if grant is None or speaker_consent_crud.effective_status(grant) != "granted":
        return "identification_consent_revoked"
    return None


@celery_app.task(name="tasks.enroll_voiceprint")
def enroll_voiceprint(
    profile_id: str,
    sample_key: str,
    filename: str,
) -> dict[str, str]:
    """Compute + store a speaker voiceprint from a consented enrollment sample.

    The embedding is encrypted at rest (:mod:`app.services.speaker_directory`).
    Runs on the transcription queue because it needs the pyannote embedding model.
    """
    from app.services import speaker_directory
    from app.services.audio_store import get_audio_store
    from app.services.diarization import (
        decode_to_wav,
        get_embedding_service,
        load_waveform,
    )

    pid = uuid.UUID(profile_id)
    wav_path: str | None = None
    audio: bytes | None = None
    try:
        # CLAIM (issue #14 review): lock the PROFILE row — the UNIVERSAL
        # serialization boundary that EVERY de-authorizing writer takes:
        # consent withdrawal (incl. speaker_identification, which does NOT touch
        # the sample's lifecycle row), voiceprint/profile erasure, and
        # retention. Under that lock, check identification consent AND the
        # sample's lifecycle status. A de-auth committed before the claim is
        # seen here (skip, no read); one that lands after is caught by the
        # recheck below, which HOLDS the profile lock across the persist so no
        # de-auth can interleave between the recheck and the commit. The lock is
        # held only across fast DB checks — the object-store read happens AFTER
        # this commits, holding no lock, so a withdrawal never waits on I/O.
        with session_scope() as session:
            from app.models.speaker_profile import SpeakerVoiceprint as _VP

            profile = speaker_profile_crud.lock_for_update_sync(session, pid)
            obj = audio_object_crud.get_by_key_sync(session, sample_key)
            # Idempotency (review round 2): the success path stamps the sample's
            # lifecycle row with the produced voiceprint id — a redelivery that
            # finds the stamp skips instead of enrolling a duplicate print.
            if (
                obj is not None
                and obj.ref_id is not None
                and session.get(_VP, obj.ref_id) is not None
            ):
                logger.info(
                    "enrollment for %s already completed; skipping duplicate",
                    sample_key,
                )
                return {"status": "duplicate", "profile_id": profile_id}
            block_reason = _enrollment_block_reason(session, pid, obj, profile)
            if block_reason is not None:
                if obj is not None and obj.status == "active":
                    audio_object_crud.request_delete_sync(
                        session, obj, reason=block_reason
                    )
                logger.info(
                    "enrollment for %s not processed (%s); skipping",
                    sample_key,
                    block_reason,
                )
                return {
                    "status": "skipped",
                    "reason": block_reason,
                    "profile_id": profile_id,
                }

        # The read runs on a CLAIMED-active, consented sample, holding no lock.
        audio = get_audio_store().get_sync(sample_key)
        wav_path = decode_to_wav(audio, src_hint=filename)
        embedder = get_embedding_service()
        vector = embedder.embed_clip_sync(load_waveform(wav_path))
        blob = speaker_directory.encrypt_embedding(vector)
        with session_scope() as session:
            # Authoritative recheck UNDER the profile lock, HELD through the
            # persist commit below: a de-auth may have committed between the
            # claim and now, and any de-auth that arrives during this block
            # blocks on the profile lock until we commit. Do not PERSIST a
            # biometric embedding without live consent + an active sample
            # (issue #14 review); tombstone the sample and skip.
            profile = speaker_profile_crud.lock_for_update_sync(session, pid)
            obj = audio_object_crud.get_by_key_sync(session, sample_key)
            block_reason = _enrollment_block_reason(session, pid, obj, profile)
            if block_reason is not None:
                if obj is not None and obj.status == "active":
                    audio_object_crud.request_delete_sync(
                        session, obj, reason=block_reason
                    )
                session.commit()
                return {
                    "status": "skipped",
                    "reason": block_reason,
                    "profile_id": profile_id,
                }
            # Keeping raw audio after embedding requires retention consent.
            # Historical voice_synthesis grants remain recognized for existing
            # records; new grants are limited to the supported consent purposes.
            keep_sample = pid in speaker_consent_crud.consented_profile_ids_sync(
                session, purpose="sample_retention"
            ) or pid in speaker_consent_crud.consented_profile_ids_sync(
                session, purpose="voice_synthesis"
            )
            from app.models.audio_object import AudioObject as _AO

            # ONE transaction (review round 3): the voiceprint and its
            # idempotency claim commit together. The claim is a CAS on the
            # sample's lifecycle row — ``ref_id`` flips from NULL to the new
            # voiceprint id only once; a concurrent or crashed-then-retried
            # delivery loses the CAS, rolls the duplicate print back, and
            # skips.
            audio_object_crud.ensure_for_key_sync(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=sample_key,
                kind="enrollment_sample",
            )
            vp = speaker_profile_crud.add_voiceprint_sync(
                session,
                profile_id=pid,
                embedding_blob=blob,
                dims=len(vector),
                model_id=embedder.model_id,
                sample_key=sample_key if keep_sample else None,
                commit=False,
            )
            claimed = session.execute(
                sa_update(_AO)
                .where(_AO.key == sample_key, _AO.ref_id.is_(None))
                .values(ref_id=vp.id)
            )
            if claimed.rowcount != 1:
                session.rollback()
                logger.info(
                    "enrollment for %s lost the idempotency claim; skipping",
                    sample_key,
                )
                return {"status": "duplicate", "profile_id": profile_id}
            if not keep_sample:
                obj = audio_object_crud.get_by_key_sync(session, sample_key)
                if obj is not None:
                    audio_object_crud.request_delete_sync(
                        session, obj, reason="no_sample_retention_consent"
                    )
            # The sent outbox row was this sample's queued-work marker for the
            # orphan-intent sweep — retire it in the same transaction that
            # lands the result (review round 4), so protection ends exactly
            # when the work does, not on an arbitrary prune clock.
            task_outbox_crud.consume_sync(session, dedupe_key=f"enroll:{sample_key}")
            session.commit()
        logger.info(
            "voiceprint enrolled",
            extra={"operation": "speaker.enroll", "profile_id": profile_id},
        )
        return {"status": "enrolled", "profile_id": profile_id}
    except Exception:  # noqa: BLE001
        logger.exception("voiceprint enrollment failed for %s", profile_id)
        # A failed enrollment must not leak its raw sample: tombstone it for the
        # durable deletion sweep (best-effort here; the row may already exist).
        with contextlib.suppress(Exception), session_scope() as session:
            obj = audio_object_crud.ensure_for_key_sync(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=sample_key,
                kind="enrollment_sample",
                ref_id=pid,
            )
            audio_object_crud.request_delete_sync(
                session, obj, reason="failed_enrollment"
            )
            session.commit()
        # Re-raise so Celery and the job ledger record a real FAILURE (#6).
        raise
    finally:
        if wav_path:
            with contextlib.suppress(OSError):
                os.remove(wav_path)


@celery_app.task(
    name="tasks.create_meeting_clip",
    soft_time_limit=600,
    time_limit=900,
)
def create_meeting_clip(clip_id: str) -> dict[str, str]:
    """Slice one governed clip out of a recording with ffmpeg (issue #6, D5).

    Runs on the ``transcription`` queue — the stt image bundles ffmpeg via
    imageio-ffmpeg. File→file throughout (bounded memory); the result becomes
    its own ``audio_object(kind="clip")`` served by the same Range proxy.
    """
    import subprocess
    import tempfile

    import imageio_ffmpeg

    from app.crud import meeting_clip as meeting_clip_crud
    from app.models.meeting_clip import MeetingClip
    from app.models.meeting_recording import MeetingRecording
    from app.services import audio_store as audio_store_service

    cid = uuid.UUID(clip_id)
    with session_scope() as session:
        if not meeting_clip_crud.claim_processing_sync(session, clip_id=cid):
            logger.info("create_meeting_clip: clip %s not pending", clip_id)
            return {"status": "skipped", "reason": "not_pending"}
        clip = session.get(MeetingClip, cid)
        assert clip is not None  # the CAS above just matched this row
        recording = session.get(MeetingRecording, clip.recording_id)
        try:
            if recording is None or not recording.audio_key or recording.audio_deleted:
                raise RuntimeError("source audio is no longer available")
            store = audio_store_service.get_audio_store()
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            with tempfile.TemporaryDirectory(prefix="clip-") as tmp:
                src = os.path.join(tmp, "source")
                out = os.path.join(tmp, "clip.webm")
                store.download_to_path(recording.audio_key, src)
                # -ss/-to AFTER -i: sample-accurate cut (decode-seek); audio
                # re-encodes to opus so any source codec yields a clean clip.
                result = subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-i",
                        src,
                        "-ss",
                        f"{clip.start_ms / 1000:.3f}",
                        "-to",
                        f"{clip.end_ms / 1000:.3f}",
                        "-vn",
                        # An online meeting has the microphone in one channel
                        # and the call in the other. A clip is listened to
                        # outside the app, where nothing folds them: mix them
                        # here, or it plays one side in each ear.
                        *(["-ac", "1"] if recording.audio_layout == "mic_tab" else []),
                        "-c:a",
                        "libopus",
                        out,
                    ],
                    capture_output=True,
                    timeout=480,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"ffmpeg failed: {result.stderr.decode()[-500:]}"
                    )
                size = os.path.getsize(out)
                # Namespaced like every other object key (issue #14 review):
                # AUDIO_S3_KEY_PREFIX must isolate ALL keys when a bucket is
                # shared, not just recordings and voiceprints.
                key = (
                    f"{audio_store_service.key_namespace()}"
                    f"workspaces/{clip.workspace_id}/clips/{clip.id.hex}.webm"
                )
                with open(out, "rb") as fh:
                    store.put_file(key, fh, "audio/webm")
            obj = audio_object_crud.ensure_for_key_sync(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=key,
                kind="clip",
                ref_id=clip.id,
                workspace_id=clip.workspace_id,
            )
            obj.size_bytes = size
            meeting_clip_crud.complete_sync(
                session, clip_id=cid, audio_key=key, size_bytes=size
            )
            return {"status": "ready", "size": str(size)}
        except Exception as exc:
            logger.exception("create_meeting_clip: clip %s failed", clip_id)
            meeting_clip_crud.fail_sync(
                session, clip_id=cid, error=f"{type(exc).__name__}: {exc}"
            )
            raise


@celery_app.task(
    name="tasks.scan_meeting_dlp",
    # Workspace-supplied regexes are untrusted (review finding: ReDoS): a
    # catastrophic pattern is cut by the soft limit instead of pinning a
    # worker child indefinitely. CPython's regex loop checks signals, so the
    # soft-limit exception lands even mid-match.
    soft_time_limit=120,
    time_limit=180,
)
def scan_meeting_dlp(recording_id: str, revision_id: str) -> dict[str, str]:
    """Regex DLP scan of one revision's transcript (issue #6, E4).

    Idempotent: a rescan replaces that revision's PENDING findings; reviewed
    findings (accepted/dismissed/redacted) are never resurrected or doubled.
    """
    from sqlalchemy import delete as sa_delete

    from app.models.meeting_dlp import DlpFinding, DlpTerm
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.services import dlp as dlp_service

    rid = uuid.UUID(recording_id)
    rev_id = uuid.UUID(revision_id)
    with session_scope() as session:
        recording = session.get(MeetingRecording, rid)
        if recording is None:
            return {"status": "skipped", "reason": "recording_gone"}
        segments = (
            session.execute(
                select(MeetingTranscriptSegment)
                .where(MeetingTranscriptSegment.revision_id == rev_id)
                .order_by(MeetingTranscriptSegment.idx)
            )
            .scalars()
            .all()
        )
        terms = [
            (t.pattern, t.is_regex, t.label)
            for t in session.execute(
                select(DlpTerm).where(
                    DlpTerm.workspace_id == recording.workspace_id,
                    DlpTerm.active.is_(True),
                )
            )
            .scalars()
            .all()
        ]
        session.execute(
            sa_delete(DlpFinding).where(
                DlpFinding.revision_id == rev_id,
                DlpFinding.status == "pending",
            )
        )
        reviewed = {
            (f.segment_id, f.start_char, f.end_char)
            for f in session.execute(
                select(DlpFinding).where(DlpFinding.revision_id == rev_id)
            )
            .scalars()
            .all()
        }
        found = 0
        for seg in segments:
            for match in dlp_service.scan_text(seg.text, terms):
                if (seg.id, match.start_char, match.end_char) in reviewed:
                    continue
                session.add(
                    DlpFinding(
                        workspace_id=recording.workspace_id,
                        recording_id=rid,
                        revision_id=rev_id,
                        segment_id=seg.id,
                        detector=match.detector,
                        term_label=match.label,
                        matched_text=match.matched_text[:500],
                        start_char=match.start_char,
                        end_char=match.end_char,
                        start_ms=seg.start_ms,
                        end_ms=seg.end_ms,
                        status="pending",
                    )
                )
                found += 1
        session.commit()
        logger.info("scan_meeting_dlp: %s findings on revision %s", found, rev_id)
        return {"status": "completed", "findings": str(found)}


@celery_app.task(
    name="tasks.apply_meeting_redaction",
    soft_time_limit=900,
    time_limit=1200,
)
def apply_meeting_redaction(recording_id: str) -> dict[str, str]:
    """Apply the ACCEPTED findings: superseding masked revision + muted audio
    rendition + object swap (issue #6, E4).

    Never mutates: the source revision keeps its text, the original audio
    object gets a delete_requested tombstone (unless a legal hold applies) and
    playback swaps to the muted rendition. Runs on the transcription queue for
    ffmpeg. Audio work happens BEFORE any DB mutation so an ffmpeg failure
    leaves the findings accepted and nothing half-redacted.
    """
    from app.models.meeting_dlp import DlpFinding
    from app.models.meeting_recording import MeetingRecording

    rid = uuid.UUID(recording_id)
    with session_scope() as session:
        recording = session.get(MeetingRecording, rid)
        if recording is None:
            return {"status": "skipped", "reason": "recording_gone"}
        source_revision_id = (
            recording.approved_revision_id or recording.latest_revision_id
        )
        # CLAIM the accepted findings (review round 2): flipping them
        # accepted→redacting is the job CAS — a concurrent/duplicate delivery
        # claims zero rows and no-ops instead of redacting twice. A failed
        # run reverts them to accepted (and the stale sweep backstops a
        # crashed worker).
        # RETURNING makes the claim OWNED (review round 3): this worker
        # processes exactly the ids ITS update flipped — findings claimed by
        # a concurrent delivery, or accepted after this claim, are never
        # touched (neither in processing nor in failure cleanup).
        claimed_ids = [
            row[0]
            for row in session.execute(
                sa_update(DlpFinding)
                .where(
                    DlpFinding.recording_id == rid,
                    DlpFinding.revision_id == source_revision_id,
                    DlpFinding.status == "accepted",
                )
                .values(status="redacting")
                .returning(DlpFinding.id)
            )
        ]
        session.commit()
        if not claimed_ids:
            return {"status": "skipped", "reason": "nothing_accepted"}
        findings = (
            session.execute(select(DlpFinding).where(DlpFinding.id.in_(claimed_ids)))
            .scalars()
            .all()
        )
        try:
            return _apply_redaction_claimed(
                session, recording, findings, source_revision_id, rid
            )
        except Exception:
            # Release ONLY this worker's claim so the user can retry (review
            # round 3: the session may be dirty from the failed finalize —
            # roll it back before the release commit, and touch only the ids
            # this worker owns).
            session.rollback()
            session.execute(
                sa_update(DlpFinding)
                .where(
                    DlpFinding.id.in_(claimed_ids),
                    DlpFinding.status == "redacting",
                )
                .values(status="accepted")
            )
            session.commit()
            raise


def _apply_redaction_claimed(
    session, recording, findings, source_revision_id, rid
) -> dict[str, str]:
    import subprocess
    import tempfile

    from app.models.meeting_transcript_revision import MeetingTranscriptRevision
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.services import audio_store as audio_store_service
    from app.services import dlp as dlp_service
    from app.services import retention

    segments = (
        session.execute(
            select(MeetingTranscriptSegment)
            .where(MeetingTranscriptSegment.revision_id == source_revision_id)
            .order_by(MeetingTranscriptSegment.idx)
        )
        .scalars()
        .all()
    )
    # 1) Muted audio rendition FIRST (the fallible external step).
    new_key: str | None = None
    new_size: int | None = None
    mute_ranges = sorted({(f.start_ms / 1000.0, f.end_ms / 1000.0) for f in findings})
    if recording.audio_key and not recording.audio_deleted:
        import imageio_ffmpeg

        store = audio_store_service.get_audio_store()
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        volume_filter = ", ".join(
            f"volume=enable='between(t,{start:.3f},{end:.3f})':volume=0"
            for start, end in mute_ranges
        )
        with tempfile.TemporaryDirectory(prefix="redact-") as tmp:
            src = os.path.join(tmp, "source")
            out = os.path.join(tmp, "redacted.webm")
            store.download_to_path(recording.audio_key, src)
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    src,
                    "-vn",
                    "-af",
                    volume_filter,
                    "-c:a",
                    "libopus",
                    out,
                ],
                capture_output=True,
                timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()[-500:]}")
            new_size = os.path.getsize(out)
            new_key = (
                f"{audio_store_service.key_namespace()}"
                f"workspaces/{recording.workspace_id}/redacted/"
                f"{uuid.uuid4().hex}.webm"
            )
            # Intent-first (review round 3): the lifecycle row commits
            # BEFORE the store write, so a crash between the put and the
            # finalize transaction can never leave untracked personal
            # audio — the orphan-intent sweep reclaims a row whose
            # ref_id was never stamped.
            audio_object_crud.ensure_for_key_sync(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=new_key,
                kind="redacted_audio",
                workspace_id=recording.workspace_id,
            )
            session.commit()
            try:
                with open(out, "rb") as fh:
                    store.put_file(new_key, fh, "audio/webm")
            except Exception:
                intent = audio_object_crud.get_by_key_sync(session, new_key)
                if intent is not None:
                    audio_object_crud.request_delete_sync(
                        session, intent, reason="redaction_upload_failed"
                    )
                    session.commit()
                raise

    # 2) One transaction: masked superseding revision + pointer swaps +
    #    finding status + original-object tombstone.
    spans_by_segment: dict[uuid.UUID, list[tuple[int, int]]] = {}
    for f in findings:
        spans_by_segment.setdefault(f.segment_id, []).append((f.start_char, f.end_char))
    seg_rows = []
    for seg in segments:
        text = seg.text
        spans = spans_by_segment.get(seg.id)
        if spans:
            text = dlp_service.mask_spans(text, spans)
        seg_rows.append(
            {
                "idx": seg.idx,
                "start_ms": seg.start_ms,
                "end_ms": seg.end_ms,
                "speaker_label": seg.speaker_label,
                "speaker_profile_id": seg.speaker_profile_id,
                "speaker_display_name": seg.speaker_display_name,
                "speaker_confidence": seg.speaker_confidence,
                "text": text,
            }
        )
    revision = transcript_revision_crud.create_revision_sync(
        session,
        recording_id=rid,
        attempt_id=None,
        origin="redaction",
        seg_rows=seg_rows,
    )
    # A redaction supersedes immediately — leaving the unredacted text
    # authoritative while someone reviews would defeat the point.
    now = datetime.now(UTC)
    previous = session.get(MeetingTranscriptRevision, source_revision_id)
    if previous is not None and previous.state == "approved":
        previous.state = "superseded"
        # Flush the supersede FIRST: the one-approved-per-recording
        # partial unique index would otherwise see two approved rows.
        session.flush()
    revision.state = "approved"
    revision.approved_at = now
    recording.approved_revision_id = revision.id
    for f in findings:
        f.status = "redacted"
    if new_key is not None:
        old_key = recording.audio_key
        rendition = audio_object_crud.ensure_for_key_sync(
            session,
            bucket=settings.AUDIO_S3_BUCKET,
            key=new_key,
            kind="redacted_audio",
            workspace_id=recording.workspace_id,
        )
        rendition.ref_id = rid  # the finalize stamp completes the intent
        rendition.size_bytes = new_size
        recording.audio_key = new_key
        recording.size_bytes = new_size
        # The rendition is always WebM/Opus regardless of the source
        # container (review finding: stale MIME broke playback).
        recording.audio_content_type = "audio/webm"
        held = retention.is_held_sync(
            session,
            ("recording", rid),
            ("workspace", recording.workspace_id),
        )
        if old_key and not held:
            old_obj = audio_object_crud.get_by_key_sync(session, old_key)
            if old_obj is not None:
                audio_object_crud.request_delete_sync(
                    session, old_obj, reason="redacted"
                )
    session.commit()
    return {
        "status": "completed",
        "revision": str(revision.revision_number),
        "muted_ranges": str(len(mute_ranges)),
    }


@celery_app.task(
    name="tasks.build_compliance_export",
    soft_time_limit=600,
    time_limit=900,
)
def build_compliance_export(job_id: str) -> dict[str, str]:
    """Build one DSAR bundle (issue #6, E5): everything the meetings domain
    holds about the subject, zipped and stored as an export_bundle object.

    Voiceprint EMBEDDINGS are never exported — only their metadata; the
    embedding is the biometric itself, and a DSAR is about transparency, not
    another copy of the biometric leaving the system.
    """
    import io
    import json as json_module
    import zipfile

    from app.models.compliance_export import ComplianceExportJob
    from app.models.meeting_governance import MeetingAuditEvent
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.models.speaker_consent import SpeakerConsentRecord
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
    from app.models.user import User as UserModel
    from app.services import audio_store as audio_store_service

    jid = uuid.UUID(job_id)

    def _iso(value) -> str | None:
        return value.isoformat() if value is not None else None

    with session_scope() as session:
        claimed = session.execute(
            sa_update(ComplianceExportJob)
            .where(
                ComplianceExportJob.id == jid,
                ComplianceExportJob.status == "pending",
            )
            .values(status="running", updated_at=datetime.now(UTC))
        )
        session.commit()
        if claimed.rowcount != 1:
            return {"status": "skipped", "reason": "not_pending"}
        job = session.get(ComplianceExportJob, jid)
        assert job is not None
        try:
            subject = session.execute(
                select(UserModel).where(UserModel.email == job.subject_email)
            ).scalar_one_or_none()

            profiles = (
                session.execute(
                    select(SpeakerProfile).where(
                        (SpeakerProfile.id == job.speaker_profile_id)
                        if job.speaker_profile_id is not None
                        else (
                            SpeakerProfile.user_id == subject.id
                            if subject is not None
                            else SpeakerProfile.id.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            profile_ids = [p.id for p in profiles]

            voiceprints = (
                session.execute(
                    select(SpeakerVoiceprint).where(
                        SpeakerVoiceprint.speaker_profile_id.in_(profile_ids)
                    )
                )
                .scalars()
                .all()
                if profile_ids
                else []
            )
            consents = (
                session.execute(
                    select(SpeakerConsentRecord).where(
                        SpeakerConsentRecord.profile_id.in_(profile_ids)
                    )
                )
                .scalars()
                .all()
                if profile_ids
                else []
            )

            # Appearances: which recordings/revisions/time ranges the subject's
            # voice was identified in.
            appearances: list[dict] = []
            if profile_ids:
                rows = session.execute(
                    select(MeetingTranscriptSegment).where(
                        MeetingTranscriptSegment.speaker_profile_id.in_(profile_ids)
                    )
                ).scalars()
                grouped: dict[tuple, dict] = {}
                for seg in rows:
                    key = (seg.recording_id, seg.revision_id)
                    entry = grouped.setdefault(
                        key,
                        {
                            "recording_id": str(seg.recording_id),
                            "revision_id": str(seg.revision_id),
                            "segments": 0,
                            "first_ms": seg.start_ms,
                            "last_ms": seg.end_ms,
                        },
                    )
                    entry["segments"] += 1
                    entry["first_ms"] = min(entry["first_ms"], seg.start_ms)
                    entry["last_ms"] = max(entry["last_ms"], seg.end_ms)
                for (rec_id, _), entry in grouped.items():
                    recording = session.get(MeetingRecording, rec_id)
                    entry["title"] = recording.title if recording else None
                    appearances.append(entry)

            # Deletion evidence: sample objects for the subject's voiceprints +
            # audio objects of the recordings they appear in.
            sample_keys = [vp.sample_key for vp in voiceprints if vp.sample_key]
            recording_ids = list({uuid.UUID(a["recording_id"]) for a in appearances})
            evidence_objects = (
                session.execute(
                    select(AudioObject).where(
                        AudioObject.key.in_(sample_keys)
                        | AudioObject.ref_id.in_(recording_ids)
                    )
                )
                .scalars()
                .all()
                if (sample_keys or recording_ids)
                else []
            )

            audit_events = (
                session.execute(
                    select(MeetingAuditEvent)
                    .where(MeetingAuditEvent.actor_email == job.subject_email)
                    .order_by(MeetingAuditEvent.created_at)
                )
                .scalars()
                .all()
            )

            bundle = {
                "manifest.json": {
                    "export_job_id": str(job.id),
                    "subject_email": job.subject_email,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "requested_by": job.requested_by_email,
                    "contents": [
                        "consents.json",
                        "voiceprints.json",
                        "appearances.json",
                        "deletion_evidence.json",
                        "audit_events.json",
                    ],
                    "note": (
                        "Voiceprint embeddings are intentionally excluded: "
                        "only their metadata is exported."
                    ),
                },
                "consents.json": [
                    {
                        "profile_id": str(c.profile_id),
                        "purpose": c.purpose,
                        "status": c.status,
                        "policy_version": c.policy_version,
                        "on_behalf": c.on_behalf,
                        "actor_email": c.actor_email,
                        "granted_at": _iso(c.created_at),
                        "expires_at": _iso(c.expires_at),
                        "withdrawn_at": _iso(c.withdrawn_at),
                        "withdrawal_reason": c.withdrawal_reason,
                    }
                    for c in consents
                ],
                "voiceprints.json": [
                    {
                        "profile_id": str(vp.speaker_profile_id),
                        "model_id": vp.model_id,
                        "model_revision": vp.model_revision,
                        "dims": vp.dims,
                        "key_id": vp.key_id,
                        "sample_stored": vp.sample_key is not None,
                        "created_at": _iso(vp.created_at),
                    }
                    for vp in voiceprints
                ],
                "appearances.json": appearances,
                "deletion_evidence.json": [
                    {
                        "kind": o.kind,
                        "status": o.status,
                        "delete_reason": o.delete_reason,
                        "delete_requested_at": _iso(o.delete_requested_at),
                        "deleted_at": _iso(o.deleted_at),
                        # Cumulative store-delete effort across all cycles.
                        "total_delete_attempts": o.total_delete_attempts,
                        "deleted_version_ids": o.deleted_version_ids or [],
                    }
                    for o in evidence_objects
                ],
                "audit_events.json": [
                    {
                        "action": e.action,
                        "workspace_id": (
                            str(e.workspace_id) if e.workspace_id else None
                        ),
                        "recording_id": (
                            str(e.recording_id) if e.recording_id else None
                        ),
                        "target": e.target,
                        "at": _iso(e.created_at),
                    }
                    for e in audit_events
                ],
            }
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, payload in bundle.items():
                    zf.writestr(
                        name, json_module.dumps(payload, indent=2, sort_keys=True)
                    )
            data = buffer.getvalue()

            store = audio_store_service.get_audio_store()
            key = f"{audio_store_service.key_namespace()}compliance/{job.id.hex}.zip"
            buffer.seek(0)
            store.put_file(key, io.BytesIO(data), "application/zip")
            audio_object_crud.ensure_for_key_sync(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=key,
                kind="export_bundle",
                ref_id=job.id,
            ).size_bytes = len(data)
            job.audio_key = key
            job.size_bytes = len(data)
            job.status = "completed"
            job.error = None
            session.commit()
            return {"status": "completed", "size": str(len(data))}
        except Exception as exc:
            logger.exception("build_compliance_export: job %s failed", job_id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
            raise


#: How long the audio of a study note that failed transcription is kept.
STUDY_AUDIO_KEEP_DAYS = 7


@celery_app.task(name="tasks.cleanup_old_audio")
def cleanup_old_audio() -> dict[str, int]:
    """Retention sweep: request durable deletion of aged meeting audio + purge
    expired voiceprints.

    This task never calls the object store itself (issue #6): it records a
    ``delete_requested`` tombstone per blob and lets ``process_audio_deletions``
    drive it to a **confirmed** deletion — only then are ``audio_key`` /
    ``audio_deleted`` updated. Expired voiceprint rows (the encrypted
    embeddings) are erased immediately; their sample objects get tombstones.
    Retention OUTLIVES feature enablement (issue #14 review): data already
    collected must still be deleted per policy after the feature is switched
    off, so the sweep always runs and is a no-op only when there is nothing
    aged to delete.
    """
    from app.models.meeting_recording import MeetingRecording
    from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint

    delete_requested = 0
    voiceprints_deleted = 0
    now = datetime.now(UTC)

    with session_scope() as session:
        rows = (
            session.execute(
                select(MeetingRecording).where(
                    MeetingRecording.audio_deleted.is_(False),
                    MeetingRecording.audio_key.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        for rec in rows:
            # Per-artifact, per-workspace retention resolution (issue #6, E3);
            # None = keep indefinitely, and a legal hold trumps everything.
            days = retention.resolve_retention_days_sync(
                session, artifact="raw_audio", workspace_id=rec.workspace_id
            )
            if days is None:
                continue
            created = rec.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created >= now - timedelta(days=days):
                continue
            if retention.is_held_sync(
                session,
                ("recording", rec.id),
                ("workspace", rec.workspace_id),
            ):
                continue
            obj = audio_object_crud.ensure_for_key_sync(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=rec.audio_key,
                kind="meeting_recording",
                ref_id=rec.id,
                workspace_id=rec.workspace_id,
            )
            audio_object_crud.request_delete_sync(session, obj, reason="retention")
            delete_requested += 1
        # Study-note excerpts are deleted when transcribed; one that failed and
        # was never retried must not stay forever.
        from app.models.study import StudyNote

        stale = now - timedelta(days=STUDY_AUDIO_KEEP_DAYS)
        for key in session.execute(
            select(StudyNote.audio_key).where(
                StudyNote.audio_key.is_not(None), StudyNote.updated_at < stale
            )
        ).scalars():
            obj = audio_object_crud.get_by_key_sync(session, key)
            if obj is not None and obj.status == "active":
                audio_object_crud.request_delete_sync(session, obj, reason="retention")
                delete_requested += 1
        session.commit()

    # Erase voiceprints past their profile's retention; tombstone their samples.
    # SORTED profile ids + one BOUNDED transaction per profile (issue #14
    # review): take the profile lock — the universal serialization boundary a
    # queued enrollment/render worker also takes — so this erasure cannot
    # straddle an in-flight raw-sample read, and overlapping runs order their
    # locks identically.
    with session_scope() as session:
        expired_profile_ids = sorted(
            session.execute(
                select(SpeakerProfile.id).where(
                    SpeakerProfile.retention_delete_at.is_not(None),
                    SpeakerProfile.retention_delete_at < now,
                )
            )
            .scalars()
            .all()
        )
    for pid_ in expired_profile_ids:
        with session_scope() as session:
            speaker_profile_crud.lock_for_update_sync(session, pid_)
            if retention.is_held_sync(session, ("speaker_profile", pid_)):
                continue
            expired = (
                session.execute(
                    select(SpeakerVoiceprint).where(
                        SpeakerVoiceprint.speaker_profile_id == pid_
                    )
                )
                .scalars()
                .all()
            )
            for vp in expired:
                if vp.sample_key:
                    obj = audio_object_crud.ensure_for_key_sync(
                        session,
                        bucket=settings.AUDIO_S3_BUCKET,
                        key=vp.sample_key,
                        kind="enrollment_sample",
                        ref_id=vp.id,
                    )
                    audio_object_crud.request_delete_sync(
                        session, obj, reason="retention"
                    )
                    delete_requested += 1
                session.delete(vp)
                voiceprints_deleted += 1
            session.commit()

    # Retained sample whose retention consent is no longer live (EXPIRED or
    # withdrawn) must be purged even without an explicit withdrawal request —
    # otherwise an expiring grant leaves the raw sample until the far-off
    # profile retention date (issue #14 review). Withdrawal is the immediate
    # path; this sweep is the backstop for expiry. Idempotent: only samples
    # with a still-active object are (re-)tombstoned.
    from app.models.speaker_profile import SpeakerProfile as _SP

    # A cheap read-only pass narrows to profiles that HAVE a retained sample,
    # SORTED so two overlapping sweeps take the profile locks in the same order
    # and cannot deadlock (issue #14 review).
    with session_scope() as session:
        candidate_ids = sorted(
            session.execute(
                select(SpeakerVoiceprint.speaker_profile_id)
                .where(SpeakerVoiceprint.sample_key.is_not(None))
                .distinct()
            )
            .scalars()
            .all()
        )
    # One BOUNDED transaction per profile: lock, re-check under the lock,
    # tombstone, commit. Locks release immediately (not held for the whole
    # sweep), and an error on one profile neither rolls back earlier deletions
    # nor blocks consent mutations org-wide.
    for pid in candidate_ids:
        with session_scope() as session:
            session.execute(
                select(_SP).where(_SP.id == pid).with_for_update()
            ).scalar_one_or_none()
            retention_live = (
                speaker_consent_crud.current_record_sync(
                    session, profile_id=pid, purpose="sample_retention"
                ),
                speaker_consent_crud.current_record_sync(
                    session, profile_id=pid, purpose="voice_synthesis"
                ),
            )
            if any(
                r is not None and speaker_consent_crud.effective_status(r) == "granted"
                for r in retention_live
            ):
                continue
            if retention.is_held_sync(session, ("speaker_profile", pid)):
                continue
            samples = (
                session.execute(
                    select(SpeakerVoiceprint).where(
                        SpeakerVoiceprint.speaker_profile_id == pid,
                        SpeakerVoiceprint.sample_key.is_not(None),
                    )
                )
                .scalars()
                .all()
            )
            for vp in samples:
                obj = audio_object_crud.ensure_for_key_sync(
                    session,
                    bucket=settings.AUDIO_S3_BUCKET,
                    key=vp.sample_key,
                    kind="enrollment_sample",
                    ref_id=vp.id,
                )
                if obj.status == "active":
                    audio_object_crud.request_delete_sync(
                        session, obj, reason="consent_no_longer_live"
                    )
                    delete_requested += 1
            session.commit()

    logger.info(
        "audio retention sweep: %d deletion(s) requested, %d voiceprints purged",
        delete_requested,
        voiceprints_deleted,
        extra={
            "operation": "meeting.retention",
            "audio_delete_requested": delete_requested,
            "voiceprints_deleted": voiceprints_deleted,
        },
    )
    return {
        "audio_delete_requested": delete_requested,
        "voiceprints_deleted": voiceprints_deleted,
    }


def _finalize_confirmed_deletion(session, obj) -> None:
    """Propagate a CONFIRMED object deletion to its owner's metadata.

    Only here — never before store confirmation — may application rows claim
    the audio is gone.
    """
    from app.models.meeting_recording import MeetingRecording
    from app.models.speaker_profile import SpeakerVoiceprint

    if obj.kind == "meeting_recording" and obj.ref_id is not None:
        rec = session.get(MeetingRecording, obj.ref_id)
        if rec is not None and rec.audio_key == obj.key:
            rec.audio_key = None
            rec.audio_deleted = True
    elif obj.kind == "enrollment_sample":
        vp = (
            session.execute(
                select(SpeakerVoiceprint).where(SpeakerVoiceprint.sample_key == obj.key)
            )
            .scalars()
            .first()
        )
        if vp is not None:
            vp.sample_key = None
    elif obj.kind in ("study_image", "study_audio") and obj.ref_id is not None:
        from app.models.study import StudyNote

        note = session.get(StudyNote, obj.ref_id)
        if note is not None:
            if obj.kind == "study_audio" and note.audio_key == obj.key:
                note.audio_key = None
            elif obj.kind == "study_image" and note.image_key == obj.key:
                note.image_key = None


#: How long the EXPIRY sweep waits for one session's admitted writes before
#: deferring it to the next sweep. Short on purpose — the session keeps its
#: claim, so deferring costs nothing but time (issue #37 review round 18).
_SWEEP_DRAIN_TIMEOUT_SECONDS = 5.0


def _session_writers_drained_with_wait(session, session_id) -> bool:
    """Poll one session's admitted-write counter until drained or timeout."""
    from app.api.v1.routes.meetings import COMPLETION_LEASE_SECONDS

    deadline = time.monotonic() + _SWEEP_DRAIN_TIMEOUT_SECONDS
    while True:
        stale_before = datetime.now(UTC) - timedelta(seconds=COMPLETION_LEASE_SECONDS)
        if meeting_upload_crud.session_writers_drained_sync(
            session, session_id=session_id, stale_before=stale_before
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_DRAIN_POLL_SECONDS)


#: How long erasure waits for already-admitted upload writes to finish before
#: giving up on a key. A part is bounded by the negotiated part size, so this is
#: generous; exceeding it means a writer is stuck, and the deletion retrying is
#: the only honest outcome (issue #37 review round 17b).
_DRAIN_TIMEOUT_SECONDS = 30.0
_DRAIN_POLL_SECONDS = 0.5


def _drain_upload_writers(session, key: str) -> None:
    """Fence ``key`` against new writes and wait for in-flight ones to finish.

    Raises when a writer will not settle: the deletion sweep retries with
    backoff (and eventually dead-letters as `delete_failed`, key preserved),
    which is far better than confirming an erasure that a part landing a second
    later would invalidate.
    """
    from app.api.v1.routes.meetings import COMPLETION_LEASE_SECONDS

    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    fenced_total = 0
    while True:
        stale_before = datetime.now(UTC) - timedelta(seconds=COMPLETION_LEASE_SECONDS)
        fenced, drained = meeting_upload_crud.fence_key_for_deletion_sync(
            session, key=key, stale_before=stale_before
        )
        fenced_total += fenced
        if drained:
            if fenced_total:
                logger.info(
                    "fenced %d live upload session(s) before erasing %s",
                    fenced_total,
                    key,
                )
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"upload writes for {key!r} did not drain within "
                f"{_DRAIN_TIMEOUT_SECONDS:.0f}s; refusing to erase under a live writer"
            )
        time.sleep(_DRAIN_POLL_SECONDS)


@celery_app.task(name="tasks.process_audio_deletions")
def process_audio_deletions(limit: int = 200) -> dict[str, int]:
    """Drive requested audio-object deletions to confirmation or dead letter.

    The only place the application deletes audio blobs. Claims due
    ``delete_requested`` tombstones (plus stale ``deleting`` claims from a
    crashed sweep), calls the store, and either confirms the deletion — which
    also updates the owning recording/voiceprint metadata — or records the
    classified failure with exponential backoff, dead-lettering to the
    admin-visible ``delete_failed`` state after
    ``AUDIO_DELETE_MAX_ATTEMPTS``.
    """
    from app.services.audio_store import get_audio_store

    counts = {
        "processed": 0,
        "confirmed": 0,
        "failed": 0,
        "dead_lettered": 0,
        "orphaned_intents": 0,
    }
    if not settings.AUDIO_S3_ENDPOINT_URL:
        return counts  # no store configured — nothing could ever be deleted

    # Intent rows whose owner never materialized (review round 2): the
    # intent-first upload pattern commits an ``active`` row with no ref_id
    # BEFORE the store write and stamps ref_id with the owner in the next
    # transaction — a crash in between leaves this shape. After a generous
    # grace window they are unowned garbage: tombstone them so the sweep
    # below erases the blob (NoSuchKey = trivially confirmed).

    grace_cutoff = datetime.now(UTC) - timedelta(hours=6)
    with session_scope() as session:
        orphans = (
            session.execute(
                select(AudioObject).where(
                    AudioObject.status == "active",
                    AudioObject.ref_id.is_(None),
                    AudioObject.created_at < grace_cutoff,
                )
            )
            .scalars()
            .all()
        )
        # A queued-but-not-yet-processed job legitimately holds its intent in
        # exactly this shape (review round 3): enrollment samples wait with
        # ref_id NULL until the transcription worker runs. Any key referenced
        # by a live outbox row is WORK, not garbage — skip it. Scoped to the
        # task names that carry audio keys in their payloads (review round 4:
        # the unfiltered scan loaded every live payload in the deployment).
        keys_with_pending_work: set[str] = set()
        if orphans:
            live_outbox = (
                session.execute(
                    select(TaskOutbox.payload).where(
                        TaskOutbox.status.in_(("pending", "sent")),
                        TaskOutbox.task_name.in_(_KEYED_OUTBOX_TASKS),
                    )
                )
                .scalars()
                .all()
            )
            for payload in live_outbox:
                for field in ("sample_key", "key"):
                    value = (payload or {}).get(field)
                    if value:
                        keys_with_pending_work.add(value)
        swept = 0
        for orphan in orphans:
            if orphan.key in keys_with_pending_work:
                continue
            audio_object_crud.request_delete_sync(
                session, orphan, reason="orphaned_intent"
            )
            counts["orphaned_intents"] += 1
            swept += 1
        if swept:
            session.commit()

    store = get_audio_store()
    now = datetime.now(UTC)
    with session_scope() as session:
        due = audio_object_crud.claim_due_deletions_sync(
            session,
            now=now,
            limit=limit,
            stale_after_seconds=settings.AUDIO_DELETE_STALE_CLAIM_SECONDS,
        )
        for obj in due:
            counts["processed"] += 1
            try:
                with log_operation(logger, "audio.lifecycle_delete", key=obj.key):
                    # FENCE, then WAIT FOR WRITERS TO DRAIN (issue #37 r17b).
                    # `expiring` is a state the part endpoint refuses, so no NEW
                    # write can start — but a write ADMITTED a moment earlier is
                    # still running, and AWS permits that UploadPart to succeed
                    # after the abort. Erasing while one is in flight would
                    # confirm over parts written seconds later, which the object
                    # HEAD cannot see.
                    _drain_upload_writers(session, obj.key)
                    purged_versions = store.delete_sync(obj.key)
            except Exception as exc:  # noqa: BLE001 — classified + kept durable
                dead = audio_object_crud.record_delete_failure_sync(
                    session,
                    obj,
                    error=f"{type(exc).__name__}: {exc}",
                    now=datetime.now(UTC),
                    max_attempts=settings.AUDIO_DELETE_MAX_ATTEMPTS,
                    base_backoff_seconds=settings.AUDIO_DELETE_RETRY_BASE_SECONDS,
                )
                counts["failed"] += 1
                MEETING_DELETIONS.labels(result="failed").inc()
                if dead:
                    counts["dead_lettered"] += 1
                    MEETING_DELETIONS.labels(result="dead_lettered").inc()
                    logger.error(
                        "audio deletion dead-lettered after %d attempts",
                        obj.delete_attempts,
                        extra={
                            "operation": "audio.delete_dead_letter",
                            "key": obj.key,
                            "kind": obj.kind,
                            "reason": obj.delete_reason,
                        },
                    )
                else:
                    logger.warning(
                        "audio deletion failed (attempt %d, will retry)",
                        obj.delete_attempts,
                        extra={
                            "operation": "audio.delete_retry",
                            "key": obj.key,
                            "kind": obj.kind,
                        },
                    )
                continue
            audio_object_crud.confirm_deleted_sync(
                session,
                obj,
                now=datetime.now(UTC),
                version_ids=purged_versions or None,
            )
            _finalize_confirmed_deletion(session, obj)
            counts["confirmed"] += 1
            MEETING_DELETIONS.labels(result="confirmed").inc()
        session.commit()
        backlog = session.execute(
            select(func.count())
            .select_from(AudioObject)
            .where(
                AudioObject.status.in_(
                    ("delete_requested", "deleting", "delete_failed")
                )
            )
        ).scalar()
        MEETING_DELETION_BACKLOG.set(int(backlog or 0))

    if counts["processed"]:
        logger.info(
            "audio deletion sweep: %(confirmed)d confirmed, %(failed)d failed",
            counts,
            extra={"operation": "audio.delete_sweep", **counts},
        )
    return counts


# --- Voiceprint encryption maintenance -------------------------------------


@celery_app.task(name="tasks.reencrypt_voiceprints")
def reencrypt_voiceprints() -> dict[str, int]:
    """Rotate voiceprint encryption onto the active biometric key (issue #6).

    Admin-triggered after adding a new key to ``BIOMETRIC_KEYS`` and pointing
    ``BIOMETRIC_ACTIVE_KEY_ID`` at it: every print still encrypted under
    another key is decrypted with ITS recorded key and re-encrypted under the
    active one. Prints that no longer decrypt are left untouched (and counted)
    — rotation must never silently destroy biometric data.
    """
    from app.models.speaker_profile import SpeakerVoiceprint
    from app.services import speaker_directory

    active = speaker_directory.active_key_id()
    rotated = 0
    failed = 0
    with session_scope() as session:
        rows = (
            session.execute(
                select(SpeakerVoiceprint).where(SpeakerVoiceprint.key_id != active)
            )
            .scalars()
            .all()
        )
        for vp in rows:
            try:
                vector = speaker_directory.decrypt_embedding(
                    vp.embedding, vp.dims, key_id=vp.key_id
                )
            except ValueError:
                failed += 1
                logger.error(
                    "voiceprint could not be re-encrypted (undecryptable)",
                    extra={
                        "operation": "speaker.reencrypt_failed",
                        "voiceprint_id": str(vp.id),
                        "key_id": vp.key_id,
                    },
                )
                continue
            vp.embedding = speaker_directory.encrypt_embedding(vector)
            vp.key_id = active
            rotated += 1
        session.commit()
    logger.info(
        "voiceprint key rotation: %d rotated, %d failed",
        rotated,
        failed,
        extra={
            "operation": "speaker.reencrypt",
            "rotated": rotated,
            "failed": failed,
            "active_key_id": active,
        },
    )
    return {"rotated": rotated, "failed": failed}


@celery_app.task(name="tasks.expire_upload_sessions")
def expire_upload_sessions() -> dict[str, int]:
    """Abort abandoned multipart upload sessions past their TTL (issue #6).

    An abandoned session would otherwise hold its uploaded parts in the object
    store forever. Abort failures are left for the next sweep (never marked
    expired without the store confirming the abort).
    """
    from app.models.meeting_recording import MeetingRecording
    from app.services.audio_store import (
        MultipartDisposition,
        discard_multipart,
        get_audio_store,
    )

    if not settings.AUDIO_S3_ENDPOINT_URL:
        return {"expired": 0}
    store = get_audio_store()
    expired = 0
    now = datetime.now(UTC)

    with session_scope() as session:
        # A completion holding a FRESH lease is left alone: expiry and a
        # legitimate in-flight completion can coincide near the TTL boundary,
        # and aborting there would destroy a live upload (issue #37 r11).
        from app.api.v1.routes.meetings import COMPLETION_LEASE_SECONDS

        lease_cutoff = now - timedelta(seconds=COMPLETION_LEASE_SECONDS)
        for upload in meeting_upload_crud.list_expired_sync(
            session, now=now, lease_cutoff=lease_cutoff
        ):
            # CLAIM the row before touching storage (issue #37 review r12).
            # Filtering fresh leases in the SELECT above is not enough: between
            # that read and the abort below, a client can reclaim the stale
            # lease and start a legitimate completion — and this sweep would
            # abort its multipart upload and delete the recording. The claim
            # closes that window; a reclaim that lands first makes it fail.
            # Stamped PER ROW. A sweep with many sessions (each doing a HEAD
            # and an abort against the store) would otherwise hand its later
            # rows a lease minted at task start — already stale by the time it
            # gets to them, so the fenced write below would fail for no reason
            # (issue #37 review round 13).
            claim_at = datetime.now(UTC)
            lease = meeting_upload_crud.claim_for_expiry_sync(
                session,
                session_id=upload.id,
                now=claim_at,
                lease_cutoff=claim_at - timedelta(seconds=COMPLETION_LEASE_SECONDS),
            )
            if lease is None:
                continue
            # DRAIN admitted part writes before touching storage (issue #37
            # r18). The claim stops NEW writes — admission requires `active` —
            # but a write admitted a moment earlier is still streaming, and
            # AWS permits that UploadPart to succeed after the abort. Expiring
            # the session (and cascading the recording) would then orphan the
            # late parts with nothing owning the key. The session keeps its
            # `expiring` claim; once the claim goes stale the next sweep
            # re-lists and re-takes it, by which time the writer has finished
            # or been reaped as dead.
            if not _session_writers_drained_with_wait(session, upload.id):
                logger.warning(
                    "upload session %s still has admitted writes in flight; "
                    "leaving it for the next sweep",
                    upload.id,
                )
                continue
            # A session stuck in "completing" crashed BETWEEN the external S3
            # complete and the DB registration (review finding). If the object
            # actually exists, abort would raise NoSuchUpload and the old code
            # treated that as clean expiry — permanently orphaning a completed
            # blob. Reconcile instead: register the object and tombstone it
            # through the confirmed-deletion lifecycle.
            #
            # The whole ordering lives in `discard_multipart` (issue #37 review
            # round 15) so cancellation and this sweep cannot drift apart: it
            # probes, aborts, and treats the abort's "already gone" as a
            # QUESTION — a completion that lands between the two would
            # otherwise be read as "nothing there" and orphan the object.
            # An indeterminate store raises and the session is retried.
            if upload.s3_upload_id is None:
                # Intent-first crash gap (review round 5): the row committed
                # but the id was never stamped, so there is nothing
                # addressable to abort by id. Expiring deletes the uploading
                # recording, whose CASCADE takes this session row WITH it —
                # destroying the only ownership evidence for the key (review
                # round 6). Register a durable tombstone FIRST: audio_object
                # rows survive every cascade, keep the key attributable, and
                # the deletion sweep's delete_sync both confirms the (absent)
                # object and aborts any store-side multipart upload of the
                # key from the create gap.
                disposition = MultipartDisposition.NEEDS_TOMBSTONE
                reason = "expired_upload_intent"
            else:
                reason = "orphaned_upload_completion"
                try:
                    disposition = discard_multipart(
                        store, upload.key, upload.s3_upload_id
                    )
                except Exception:  # noqa: BLE001 — retried by the next sweep
                    logger.warning(
                        "cannot reconcile expired upload session %s", upload.id
                    )
                    session.rollback()
                    continue
            if disposition is MultipartDisposition.NEEDS_TOMBSTONE:
                obj = audio_object_crud.ensure_for_key_sync(
                    session,
                    bucket=settings.AUDIO_S3_BUCKET,
                    key=upload.key,
                    kind="meeting_recording",
                    ref_id=upload.recording_id,
                    workspace_id=upload.workspace_id,
                )
                audio_object_crud.request_delete_sync(session, obj, reason=reason)
            # FENCED: the HEAD and abort above run with the store's own
            # timeouts and retries and can outlive this claim, at which point
            # another sweep legitimately reclaims the row. Whoever still holds
            # the lease writes the terminal state; the other abandons rather
            # than deleting a recording it no longer owns (r13).
            if not meeting_upload_crud.finalize_expiry_sync(
                session, session_id=upload.id, lease=lease
            ):
                session.rollback()
                logger.warning(
                    "expiry lease lost before the terminal write for session %s",
                    upload.id,
                )
                continue
            MEETING_UPLOADS.labels(result="expired").inc()
            rec = session.get(MeetingRecording, upload.recording_id)
            if rec is not None and rec.status == "uploading":
                session.delete(rec)
            session.commit()
            expired += 1
        session.commit()

        # Reconcile store-side multipart uploads the DB never learned about
        # (review round 2): a crash between the intent-first session commit
        # and the upload-id stamp strands a store-side upload no row names.
        # Abort only uploads THIS deployment can prove it owns: the upload's
        # EXACT key must appear in our own rows (upload sessions, recordings,
        # or audio-object ledger). Key membership — not workspace-uuid
        # parsing — is the ownership test (review round 5: a deployment
        # created from a database clone shares workspace uuids with its
        # source, but every upload key embeds a fresh uuid4, so exact keys
        # never collide). Intent-first creation guarantees each of our
        # uploads has such a row; anything unattributable is skipped, never
        # aborted. The listing is STREAMED page by page with chunked
        # membership lookups, so there is no truncation cap to starve
        # entries past it (review round 5).
        ttl = timedelta(hours=settings.MEETING_UPLOAD_SESSION_TTL_HOURS)
        from app.models.meeting_recording import MeetingRecording as _Rec
        from app.models.meeting_upload_session import (
            LIVE_UPLOAD_SESSION_STATUSES,
            MeetingUploadSession,
        )
        from app.services.audio_store import key_namespace

        def _owned_keys(keys: list[str]) -> tuple[set[str], set[str]]:
            """(owned, live) membership for one chunk of listed keys — all
            queries are ``key IN (...)`` probes against indexed columns, so
            database work is bounded by the chunk, not the table (round 6:
            the up-front full live-key load scanned everything every sweep).
            """
            owned: set[str] = set()
            for column in (
                _Rec.audio_key,
                AudioObject.key,
            ):
                owned.update(
                    row[0]
                    for row in session.execute(
                        select(column).where(column.in_(keys))
                    ).all()
                )
            live: set[str] = set()
            for key_value, session_status in session.execute(
                select(MeetingUploadSession.key, MeetingUploadSession.status).where(
                    MeetingUploadSession.key.in_(keys)
                )
            ).all():
                owned.add(key_value)
                # EVERY live state, not just active/completing: `expiring` and
                # `cancelling` are fresh claims held by another actor that is
                # already destroying this upload, and treating them as dead
                # would let this reconciler abort the multipart upload
                # underneath them (issue #37 review round 13).
                if session_status in LIVE_UPLOAD_SESSION_STATUSES:
                    live.add(key_value)
            return owned, live

        def _reconcile_chunk(chunk: list[dict]) -> int:
            aborted = 0
            owned, live_keys = _owned_keys([entry["key"] for entry in chunk])
            for entry in chunk:
                initiated = entry.get("initiated")
                if initiated is None:
                    # Unknown age ⇒ NOT provably stale (review round 3: the
                    # old code treated it as immediately abortable).
                    continue
                if initiated.tzinfo is None:
                    initiated = initiated.replace(tzinfo=UTC)
                if (
                    entry["key"] not in owned
                    or entry["key"] in live_keys
                    or now - initiated < ttl
                ):
                    continue
                try:
                    store.abort_multipart(entry["key"], entry["upload_id"])
                    aborted += 1
                    logger.info(
                        "aborted orphaned multipart upload",
                        extra={
                            "operation": "meeting.upload_orphan_abort",
                            "key": entry["key"],
                        },
                    )
                except Exception:  # noqa: BLE001 — retried by the next sweep
                    logger.warning(
                        "failed to abort orphaned multipart upload %s",
                        entry["key"],
                    )
            return aborted

        chunk: list[dict] = []
        try:
            for upload_entry in store.iter_multipart_uploads(
                prefix=f"{key_namespace()}workspaces/"
            ):
                chunk.append(upload_entry)
                if len(chunk) >= 500:
                    expired += _reconcile_chunk(chunk)
                    chunk = []
        except Exception:  # noqa: BLE001 — reconciliation retries next sweep
            logger.warning("could not list multipart uploads for reconciliation")
        if chunk:
            expired += _reconcile_chunk(chunk)
    if expired:
        logger.info(
            "expired %d abandoned upload session(s)",
            expired,
            extra={"operation": "meeting.upload_expire", "expired": expired},
        )
    return {"expired": expired}


# --- #22: provider-response ledger sweep + reconciliation --------------------


@celery_app.task(
    name="tasks.generate_meeting_note",
    # A long meeting is several model runs (parts + merge), each bounded by
    # NOTES_TIMEOUT_SECONDS in the agent runner.
    soft_time_limit=3 * 3600,
    time_limit=3 * 3600 + 300,
)
def generate_meeting_note(note_id: str) -> dict[str, str]:
    """Have a note agent write a document from a recording's transcript.

    Runs on the default queue: the work happens in the agent-runner container
    (CUGA + the LLM); this task only reads the transcript and stores the result.
    """
    from app.crud import meeting_note as meeting_note_crud
    from app.models.meeting_note import MeetingNote
    from app.models.meeting_recording import MeetingRecording
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.models.speaker_profile import SpeakerProfile
    from app.services import agent_runner, meeting_notes
    from app.services.transcript_export import ExportSegment

    nid = uuid.UUID(note_id)
    started = time.monotonic()
    with session_scope() as session:
        if not meeting_note_crud.claim_processing_sync(session, note_id=nid):
            logger.info("generate_meeting_note: note %s not pending", note_id)
            return {"status": "skipped", "reason": "not_pending"}
        note = session.get(MeetingNote, nid)
        assert note is not None  # the CAS above just matched this row
        try:
            recording = session.get(MeetingRecording, note.recording_id)
            if recording is None or note.revision_id is None:
                raise agent_runner.AgentRunError(
                    "The transcript is no longer available."
                )
            segments = (
                session.execute(
                    select(MeetingTranscriptSegment)
                    .where(MeetingTranscriptSegment.revision_id == note.revision_id)
                    .order_by(MeetingTranscriptSegment.idx)
                )
                .scalars()
                .all()
            )
            if not segments:
                raise agent_runner.AgentRunError("The transcript is empty.")
            profile_ids = {
                s.speaker_profile_id for s in segments if s.speaker_profile_id
            }
            names = (
                dict(
                    session.execute(
                        select(SpeakerProfile.id, SpeakerProfile.display_name).where(
                            SpeakerProfile.id.in_(profile_ids)
                        )
                    ).all()
                )
                if profile_ids
                else {}
            )
            lines = meeting_notes.transcript_lines(
                [
                    ExportSegment(
                        start_ms=s.start_ms,
                        end_ms=s.end_ms,
                        speaker=s.speaker_display_name
                        or names.get(s.speaker_profile_id, s.speaker_label),
                        text=s.text,
                    )
                    for s in segments
                ]
            )
            instructions, title = note.instructions, recording.title
            runner = agent_runner.get_agent_runner()
            try:
                # Fit the parts to the model actually being served.
                max_chars = meeting_notes.input_budget_chars(
                    runner.context_tokens_sync(),
                    instructions,
                    settings.NOTES_MAX_INPUT_CHARS,
                )
            except meeting_notes.ContextTooSmall as exc:
                raise agent_runner.AgentRunError(str(exc)) from None
            answer = meeting_notes.write_note(
                runner,
                instructions=instructions,
                title=title,
                lines=lines,
                max_chars=max_chars,
                on_step=lambda step: meeting_note_crud.set_step_sync(
                    session, note_id=nid, step=step
                ),
            )
            meeting_note_crud.complete_sync(
                session,
                note_id=nid,
                markdown=answer.text,
                model=answer.model,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {"status": "ready", "chars": str(len(answer.text))}
        except agent_runner.AgentRunError as exc:
            # An expected, explainable failure: the note shows the reason. Raised
            # again so the job list reports a failed job, not a success.
            logger.warning("generate_meeting_note: note %s failed: %s", note_id, exc)
            meeting_note_crud.fail_sync(session, note_id=nid, error=str(exc))
            raise
        except Exception as exc:
            logger.exception("generate_meeting_note: note %s failed", note_id)
            meeting_note_crud.fail_sync(
                session, note_id=nid, error=f"{type(exc).__name__}: {exc}"
            )
            raise


@celery_app.task(
    name="tasks.identify_speaker_names",
    soft_time_limit=3 * 3600,
    time_limit=3 * 3600 + 300,
)
def identify_speaker_names(run_id: str) -> dict[str, str]:
    """Have the language model look for the speakers' names in a transcript.

    It only proposes: the run stores suggestions with the quote each one rests
    on, and a person accepts or dismisses them. Runs on the default queue; the
    model work happens in the agent-runner container.
    """
    from app.crud import speaker_name_run as speaker_name_run_crud
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.models.speaker_name_run import SpeakerNameRun
    from app.models.speaker_profile import SpeakerProfile
    from app.services import agent_runner, meeting_notes, speaker_names

    rid = uuid.UUID(run_id)
    started = time.monotonic()
    with session_scope() as session:
        if not speaker_name_run_crud.claim_processing_sync(session, run_id=rid):
            logger.info("identify_speaker_names: run %s not pending", run_id)
            return {"status": "skipped", "reason": "not_pending"}
        run = session.get(SpeakerNameRun, rid)
        assert run is not None  # the CAS above just matched this row
        try:
            segments = (
                session.execute(
                    select(MeetingTranscriptSegment)
                    .where(MeetingTranscriptSegment.revision_id == run.revision_id)
                    .order_by(MeetingTranscriptSegment.idx)
                )
                .scalars()
                .all()
            )
            if not segments:
                raise agent_runner.AgentRunError("The transcript is empty.")
            profile_ids = {
                s.speaker_profile_id for s in segments if s.speaker_profile_id
            }
            names = (
                dict(
                    session.execute(
                        select(SpeakerProfile.id, SpeakerProfile.display_name).where(
                            SpeakerProfile.id.in_(profile_ids)
                        )
                    ).all()
                )
                if profile_ids
                else {}
            )
            turns = [
                speaker_names.Turn(
                    label=s.speaker_label,
                    name=s.speaker_display_name or names.get(s.speaker_profile_id),
                    start_ms=s.start_ms,
                    text=s.text,
                )
                for s in segments
            ]
            runner = agent_runner.get_agent_runner()
            try:
                max_chars = meeting_notes.input_budget_chars(
                    runner.context_tokens_sync(),
                    speaker_names.INSTRUCTIONS,
                    settings.NOTES_MAX_INPUT_CHARS,
                )
            except meeting_notes.ContextTooSmall as exc:
                raise agent_runner.AgentRunError(str(exc)) from None
            suggestions, model = speaker_names.suggest_names(
                runner, turns, max_chars=max_chars
            )
            speaker_name_run_crud.complete_sync(
                session,
                run_id=rid,
                suggestions=suggestions,
                model=model,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {"status": "ready", "suggestions": str(len(suggestions))}
        except agent_runner.AgentRunError as exc:
            logger.warning("identify_speaker_names: run %s failed: %s", run_id, exc)
            speaker_name_run_crud.fail_sync(session, run_id=rid, error=str(exc))
            raise
        except Exception as exc:
            logger.exception("identify_speaker_names: run %s failed", run_id)
            speaker_name_run_crud.fail_sync(
                session, run_id=rid, error=f"{type(exc).__name__}: {exc}"
            )
            raise


def _nudge_outbox() -> None:
    """Ask for the outbox to be sent now instead of at the next beat."""
    try:
        dispatch_task_outbox.delay()
    except Exception:  # noqa: BLE001 - the beat sends it within 30 seconds
        logger.debug("could not nudge the outbox dispatcher", exc_info=True)


@celery_app.task(
    name="tasks.transcribe_study_note", soft_time_limit=900, time_limit=960
)
def transcribe_study_note(note_id: str) -> dict[str, str]:
    """Transcribe the short tab-audio excerpt of one study note.

    Independent of the meeting pipeline (no diarization, default queue), so a
    learner can keep taking notes while the full recording is still running.
    The excerpt is temporary: once transcribed, its audio is deleted. A failed
    note keeps its audio so it can be tried again.
    """
    from app.crud import study as study_crud
    from app.models.study import StudyNote, StudySession
    from app.services.audio_store import get_audio_store
    from app.services.transcription import get_transcription_service

    nid = uuid.UUID(note_id)
    with session_scope() as session:
        if not study_crud.claim_processing_sync(session, note_id=nid):
            return {"status": "skipped", "reason": "not_pending"}
        try:
            note = session.get(StudyNote, nid)
            study_session = session.get(StudySession, note.session_id)
            language = study_session.language if study_session else "auto"
            audio = get_audio_store().get_sync(note.audio_key)
            result = get_transcription_service().transcribe_sync(
                audio, "note.wav", language=None if language == "auto" else language
            )
            study_crud.complete_sync(
                session, note_id=nid, text=(result.text or "").strip()
            )
            queued = False
            if study_crud.enrich_wanted(study_session):
                # Durable follow-up: title, tidy text, terms, slide text.
                study_crud.queue_enrich_sync(session, note)
                queued = True
            if "question" in (note.tags or []):
                if note.status != "ready":
                    note.answer_status, note.answer_error = "failed", "no_speech"
                elif study_crud.answer_wanted(note):
                    # A question was marked: find it and suggest an answer.
                    study_crud.queue_answer_sync(session, note)
                    queued = True
            session.commit()
            if queued:
                _nudge_outbox()
            obj = audio_object_crud.get_by_key_sync(session, note.audio_key)
            if obj is not None:
                audio_object_crud.request_delete_sync(
                    session, obj, reason="transcribed"
                )
                session.commit()
            return {"status": note.status}
        except Exception as exc:
            logger.exception("transcribe_study_note: note %s failed", note_id)
            session.rollback()
            study_crud.fail_sync(
                session, note_id=nid, error=f"{type(exc).__name__}: {exc}"
            )
            raise


@celery_app.task(name="tasks.enrich_study_note", soft_time_limit=900, time_limit=960)
def enrich_study_note(note_id: str) -> dict[str, str]:
    """Let the language model work on one note.

    Two independent questions: what can be read in the image, and (from the
    words) a title, the tidy transcript, key terms and, for a note marked
    "unclear", a plain explanation. Each answer is checked before it is
    stored; a part that fails leaves the others in place. The transcript
    itself (``text``) is never changed.
    """
    from app.crud import study as study_crud
    from app.models.study import StudyNote
    from app.services import study_enrich
    from app.services.agent_runner import AgentRunError, get_agent_runner
    from app.services.audio_store import get_audio_store

    nid = uuid.UUID(note_id)
    with session_scope() as session:
        if not study_crud.claim_enrich_sync(session, note_id=nid):
            return {"status": "skipped", "reason": "not_pending"}
        note = session.get(StudyNote, nid)
        runner = get_agent_runner()
        problems: list[str] = []
        done = 0
        try:
            # 1. The image. A model that cannot read images says so; the note
            # then simply has no slide text.
            if note.image_key and not note.slide_text:
                try:
                    image = get_audio_store().get_sync(note.image_key)
                    if len(image) > study_enrich.MAX_IMAGE_BYTES:
                        raise AgentRunError("The image is too large to be read.")
                    answer = runner.chat_sync(
                        instructions=study_enrich.SLIDE_INSTRUCTIONS, image=image
                    )
                    note.slide_text = study_enrich.parse_slide(answer.text)
                    done += 1
                except AgentRunError as exc:
                    problems.append(f"Slide: {exc}")
            # 2. The words.
            unclear = "unclear" in (note.tags or [])
            if (note.text or "").strip() or (note.slide_text or "").strip():
                try:
                    answer = runner.chat_sync(
                        instructions=study_enrich.NOTE_INSTRUCTIONS,
                        text=study_enrich.build_input(
                            text=note.text, slide_text=note.slide_text, unclear=unclear
                        ),
                    )
                    parsed = study_enrich.parse_note(
                        answer.text, text=note.text, unclear=unclear
                    )
                    note.title = parsed.title or note.title
                    note.clean_text = parsed.clean_text
                    note.terms = parsed.terms or None
                    note.explanation = parsed.explanation or note.explanation
                    done += 1
                except (AgentRunError, ValueError) as exc:
                    problems.append(f"Text: {exc}")
            failed = bool(problems) and done == 0
            note.enrich_status = "failed" if failed else "ready"
            note.enrich_error = " ".join(problems)[:2000] or None
            session.commit()
            return {"status": note.enrich_status}
        except Exception as exc:
            logger.exception("enrich_study_note: note %s failed", note_id)
            session.rollback()
            note = session.get(StudyNote, nid)
            if note is not None:
                note.enrich_status = "failed"
                note.enrich_error = f"{type(exc).__name__}: {exc}"[:2000]
                session.commit()
            raise


@celery_app.task(
    name="tasks.answer_study_question", soft_time_limit=1500, time_limit=1560
)
def answer_study_question(note_id: str) -> dict[str, str]:
    """Find the question in a note marked "question" and suggest an answer.

    1. The model picks the question out of what was said and gives a first
       short answer; both are stored at once, so the person in the meeting
       sees something within seconds.
    2. The workspace is searched (earlier recordings the owner may read, and
       the owner's notes).
    3. If the session allows it and web search is set up, an agent researches
       the question on the web. It is given the question only.
    4. The final answer is written from those findings.

    A step that fails leaves the earlier results in place.
    """
    from app.crud import study as study_crud
    from app.models.study import StudyNote, StudyNotebook, StudySession
    from app.models.user import User
    from app.services import study_answer
    from app.services.agent_runner import AgentRunError, get_agent_runner

    nid = uuid.UUID(note_id)
    with session_scope() as session:
        if not study_crud.claim_answer_sync(session, note_id=nid):
            return {"status": "skipped", "reason": "not_pending"}
        note = session.get(StudyNote, nid)
        runner = get_agent_runner()
        try:
            if not (note.text or "").strip():
                note.answer_status, note.answer_error = "failed", "no_speech"
                session.commit()
                return {"status": "failed"}
            # 1. The question, and a first answer.
            try:
                extracted = study_answer.parse_extract(
                    runner.chat_sync(
                        instructions=study_answer.EXTRACT_INSTRUCTIONS,
                        text=study_answer.build_extract_input(
                            text=note.text, slide_text=note.slide_text
                        ),
                    ).text
                )
            except (AgentRunError, ValueError) as exc:
                note.answer_status = "failed"
                note.answer_error = str(exc)[:2000]
                session.commit()
                return {"status": "failed"}
            if not extracted.question:
                note.answer_status, note.answer_error = "failed", "no_question"
                session.commit()
                return {"status": "failed"}
            note.question = extracted.question
            note.answer_short = extracted.answer
            note.answer = None
            note.answer_sources = None
            session.commit()  # shown while the rest is worked on

            notebook = session.get(StudyNotebook, note.notebook_id)
            study_session = session.get(StudySession, note.session_id)
            owner = session.get(User, notebook.owner_id) if notebook else None
            problems: list[str] = []
            # 2. The workspace.
            findings = (
                study_answer.search_workspace(
                    session,
                    user=owner,
                    workspace_id=notebook.workspace_id,
                    terms=extracted.terms,
                    exclude_note_id=note.id,
                    exclude_recording_id=(
                        study_session.recording_id if study_session else None
                    ),
                )
                if owner is not None
                else []
            )
            # 3. The web: only the question leaves the installation.
            web_text: str | None = None
            web_sources: list[dict] = []
            if (
                study_session is not None
                and study_session.research_web
                and settings.WEB_SEARCH_ENABLED
            ):
                try:
                    researched = runner.research_sync(question=extracted.question)
                    web_text = researched.text
                    web_sources = study_answer.web_sources(list(researched.sources))
                except AgentRunError as exc:
                    problems.append(f"Web research: {exc}")
            # 4. The answer.
            if findings or web_text:
                try:
                    short, full = study_answer.parse_compose(
                        runner.chat_sync(
                            instructions=study_answer.COMPOSE_INSTRUCTIONS,
                            text=study_answer.build_compose_input(
                                question=extracted.question,
                                findings=findings,
                                web=web_text,
                            ),
                        ).text
                    )
                    note.answer_short = short or note.answer_short
                    note.answer = full
                except (AgentRunError, ValueError) as exc:
                    problems.append(f"Answer: {exc}")
                    # The research itself is still worth showing.
                    note.answer = web_text
            note.answer_sources = [
                *[
                    finding.source(f"S{number}")
                    for number, finding in enumerate(findings, start=1)
                ],
                *web_sources,
            ] or None
            note.answer_status = "ready"
            note.answer_error = " ".join(problems)[:2000] or None
            session.commit()
            return {"status": "ready"}
        except Exception as exc:
            logger.exception("answer_study_question: note %s failed", note_id)
            session.rollback()
            note = session.get(StudyNote, nid)
            if note is not None:
                note.answer_status = "failed"
                note.answer_error = f"{type(exc).__name__}: {exc}"[:2000]
                session.commit()
            raise
