"""Background work for the cleaned copy of a transcript.

Kept in its own module (with its route, model and service) so the feature can
be taken out without touching the transcription pipeline.
"""

from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy import select

from app.db.sync_session import session_scope
from app.worker.celery_app import celery_app

logger = logging.getLogger("app.worker.cleanup_tasks")


@celery_app.task(
    name="tasks.clean_transcript",
    # One model call per ~40 lines; each call has its own limit in the runner.
    soft_time_limit=3 * 3600,
    time_limit=3 * 3600 + 300,
)
def clean_transcript(cleanup_id: str) -> dict[str, str]:
    """Have the language model correct a transcript, line by line.

    Runs on the default queue: the model work happens in the agent-runner
    container; this task reads the transcript, checks every proposal and
    stores the ones that are corrections. The transcript is not changed.
    """
    from app.crud import transcript_cleanup as transcript_cleanup_crud
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment
    from app.models.speaker_profile import SpeakerProfile
    from app.models.transcript_cleanup import TranscriptCleanup
    from app.services import agent_runner, transcript_cleanup

    cid = uuid.UUID(cleanup_id)
    started = time.monotonic()
    with session_scope() as session:
        if not transcript_cleanup_crud.claim_processing_sync(session, cleanup_id=cid):
            logger.info("clean_transcript: cleanup %s not pending", cleanup_id)
            return {"status": "skipped", "reason": "not_pending"}
        cleanup = session.get(TranscriptCleanup, cid)
        assert cleanup is not None  # the CAS above just matched this row
        try:
            segments = (
                session.execute(
                    select(MeetingTranscriptSegment)
                    .where(MeetingTranscriptSegment.revision_id == cleanup.revision_id)
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
            lines = [
                transcript_cleanup.Line(
                    speaker=s.speaker_display_name
                    or names.get(s.speaker_profile_id, s.speaker_label),
                    text=s.text,
                )
                for s in segments
            ]
            result = transcript_cleanup.clean(
                agent_runner.get_agent_runner(),
                lines,
                on_step=lambda step: transcript_cleanup_crud.set_step_sync(
                    session, cleanup_id=cid, step=step
                ),
            )
            changes = [
                {
                    "segment_id": str(segment.id),
                    "original": segment.text,
                    "text": text,
                    "status": "suggested",
                }
                for segment, text in zip(segments, result.texts, strict=True)
                if text is not None
            ]
            transcript_cleanup_crud.complete_sync(
                session,
                cleanup_id=cid,
                changes=changes,
                rejected=result.rejected,
                model=result.model,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {"status": "ready", "changes": str(len(changes))}
        except agent_runner.AgentRunError as exc:
            # An expected, explainable failure: the tab shows the reason. Raised
            # again so the job list reports a failed job, not a success.
            logger.warning("clean_transcript: cleanup %s failed: %s", cleanup_id, exc)
            transcript_cleanup_crud.fail_sync(session, cleanup_id=cid, error=str(exc))
            raise
        except Exception as exc:
            logger.exception("clean_transcript: cleanup %s failed", cleanup_id)
            transcript_cleanup_crud.fail_sync(
                session, cleanup_id=cid, error=f"{type(exc).__name__}: {exc}"
            )
            raise
