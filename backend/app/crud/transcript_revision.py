"""Transcript-revision repository (issue #6).

Sync helpers drive the worker (persist a new machine_candidate revision and
carry human edits forward); async helpers back the revision API (list, approve,
discard) and the edit-event audit trail. See
:mod:`app.models.meeting_transcript_revision`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.meeting_recording import MeetingRecording
from app.models.meeting_transcript_revision import (
    MeetingTranscriptEditEvent,
    MeetingTranscriptRevision,
)
from app.models.meeting_transcript_segment import MeetingTranscriptSegment
from app.models.user import User

# A human-touched previous segment must overlap a new segment by at least this
# fraction of the longer span for its edits to carry over automatically.
CARRY_OVER_MIN_OVERLAP = 0.9


def overlap_ratio(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    """Temporal overlap of two [start, end) ms spans over the LONGER span."""
    inter = min(a_end, b_end) - max(a_start, b_start)
    if inter <= 0:
        return 0.0
    longer = max(a_end - a_start, b_end - b_start)
    return inter / longer if longer > 0 else 0.0


def display_revision_id(rec: MeetingRecording) -> uuid.UUID | None:
    """The revision the UI shows: the approved one, else the newest."""
    return rec.approved_revision_id or rec.latest_revision_id


# --- sync (Celery worker) ---


def create_revision_sync(
    session: Session,
    *,
    recording_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    origin: str,
    seg_rows: Sequence[dict],
) -> MeetingTranscriptRevision:
    """Persist a new ``machine_candidate`` revision with its segments.

    Never touches prior revisions' segments — this replaces the destructive
    ``replace_segments_sync``. Advances the recording's ``latest_revision_id``
    (the approved pointer is untouched) and stamps the attempt's
    ``produced_revision_id``.
    """
    from app.models.meeting_transcription_attempt import MeetingTranscriptionAttempt

    number = (
        session.execute(
            select(func.max(MeetingTranscriptRevision.revision_number)).where(
                MeetingTranscriptRevision.recording_id == recording_id
            )
        ).scalar()
        or 0
    ) + 1
    rec = session.get(MeetingRecording, recording_id)
    revision = MeetingTranscriptRevision(
        recording_id=recording_id,
        revision_number=number,
        parent_revision_id=rec.latest_revision_id if rec else None,
        state="machine_candidate",
        origin=origin,
        attempt_id=attempt_id,
    )
    session.add(revision)
    session.flush()
    for row in seg_rows:
        session.add(
            MeetingTranscriptSegment(
                recording_id=recording_id, revision_id=revision.id, **row
            )
        )
    if rec is not None:
        rec.latest_revision_id = revision.id
    if attempt_id is not None:
        attempt = session.get(MeetingTranscriptionAttempt, attempt_id)
        if attempt is not None:
            attempt.produced_revision_id = revision.id
    session.flush()
    return revision


def carry_over_edits_sync(
    session: Session,
    revision: MeetingTranscriptRevision,
    *,
    previous_revision_id: uuid.UUID | None,
) -> tuple[int, int]:
    """Copy human edits from the previous revision onto the new one.

    A human-touched (``is_edited``) previous segment carries its text and
    speaker assignment onto the new segment it overlaps by ≥90% of the longer
    span; anything less confident is counted as a conflict for review instead
    of guessing. Every carry/conflict is recorded as a ``merge`` edit event.
    Returns ``(carried, conflicts)`` and stamps them on the revision.
    """
    if previous_revision_id is None:
        return (0, 0)
    prev_edited = (
        session.execute(
            select(MeetingTranscriptSegment).where(
                MeetingTranscriptSegment.revision_id == previous_revision_id,
                MeetingTranscriptSegment.is_edited.is_(True),
            )
        )
        .scalars()
        .all()
    )
    if not prev_edited:
        return (0, 0)
    new_segments = (
        session.execute(
            select(MeetingTranscriptSegment)
            .where(MeetingTranscriptSegment.revision_id == revision.id)
            .order_by(MeetingTranscriptSegment.idx)
        )
        .scalars()
        .all()
    )
    carried = 0
    conflicts = 0
    for old in prev_edited:
        best: MeetingTranscriptSegment | None = None
        best_ratio = 0.0
        for candidate in new_segments:
            ratio = overlap_ratio(
                old.start_ms, old.end_ms, candidate.start_ms, candidate.end_ms
            )
            if ratio > best_ratio:
                best, best_ratio = candidate, ratio
        if best is not None and best_ratio >= CARRY_OVER_MIN_OVERLAP:
            before = {"text": best.text, "speaker_profile_id": _s(best)}
            best.text = old.text
            best.speaker_profile_id = old.speaker_profile_id
            best.speaker_confidence = old.speaker_confidence
            best.is_edited = True
            best.edited_by_id = old.edited_by_id
            carried += 1
            session.add(
                MeetingTranscriptEditEvent(
                    revision_id=revision.id,
                    segment_id=best.id,
                    segment_idx=best.idx,
                    event_type="merge",
                    before=before,
                    after={
                        "text": old.text,
                        "speaker_profile_id": _s(old),
                        "carried_from_idx": old.idx,
                    },
                )
            )
        else:
            conflicts += 1
            session.add(
                MeetingTranscriptEditEvent(
                    revision_id=revision.id,
                    segment_id=None,
                    segment_idx=old.idx,
                    event_type="merge",
                    before={"text": old.text, "speaker_profile_id": _s(old)},
                    after={"conflict": True, "best_overlap": round(best_ratio, 3)},
                )
            )
    revision.carried_edits = carried
    revision.conflict_count = conflicts
    session.flush()
    return (carried, conflicts)


# --- async (API) ---


async def list_for_recording(
    session: AsyncSession, recording_id: uuid.UUID
) -> Sequence[MeetingTranscriptRevision]:
    result = await session.execute(
        select(MeetingTranscriptRevision)
        .where(MeetingTranscriptRevision.recording_id == recording_id)
        .order_by(MeetingTranscriptRevision.revision_number.desc())
    )
    return result.scalars().all()


async def count_surviving(session: AsyncSession, recording_id: uuid.UUID) -> int:
    """How many revisions are still displayable (not discarded).

    The discard floor reads this (issue #39): dropping the last one leaves a
    ``completed`` recording whose ``latest_revision_id`` is NULL — no transcript
    to show, no approved revision, and no un-discard endpoint to undo it.
    """
    result = await session.execute(
        select(func.count())
        .select_from(MeetingTranscriptRevision)
        .where(
            MeetingTranscriptRevision.recording_id == recording_id,
            MeetingTranscriptRevision.state != "discarded",
        )
    )
    return int(result.scalar_one())


async def get_for_recording(
    session: AsyncSession, *, recording_id: uuid.UUID, revision_id: uuid.UUID
) -> MeetingTranscriptRevision | None:
    result = await session.execute(
        select(MeetingTranscriptRevision).where(
            MeetingTranscriptRevision.id == revision_id,
            MeetingTranscriptRevision.recording_id == recording_id,
        )
    )
    return result.scalar_one_or_none()


async def approve(
    session: AsyncSession,
    rec: MeetingRecording,
    revision: MeetingTranscriptRevision,
    *,
    actor: User,
    note: str | None = None,
) -> MeetingTranscriptRevision:
    """Make ``revision`` authoritative (supersedes the previous approved one).

    If the workspace source was rendered from a different revision, the sync
    state flips to ``stale`` so downstream consumers see the drift instead of
    silently using outdated content. Committed here (multi-row invariant).
    """
    previous_id = rec.approved_revision_id
    if previous_id is not None and previous_id != revision.id:
        previous = await session.get(MeetingTranscriptRevision, previous_id)
        if previous is not None:
            previous.state = "superseded"
        await session.flush()  # release the partial-unique approved slot first
    revision.state = "approved"
    revision.approved_by_id = actor.id
    revision.approved_by_email = actor.email
    revision.approved_at = datetime.now(UTC)
    revision.approval_note = note
    rec.approved_revision_id = revision.id
    session.add(
        MeetingTranscriptEditEvent(
            revision_id=revision.id,
            event_type="approve",
            after={"note": note} if note else None,
            actor_id=actor.id,
            actor_email=actor.email,
        )
    )
    await session.commit()
    await session.refresh(revision)
    return revision


async def discard(
    session: AsyncSession,
    rec: MeetingRecording,
    revision: MeetingTranscriptRevision,
    *,
    actor: User,
) -> MeetingTranscriptRevision:
    """Discard a non-approved revision; the latest pointer falls back to the
    newest surviving revision. Committed here."""
    revision.state = "discarded"
    session.add(
        MeetingTranscriptEditEvent(
            revision_id=revision.id,
            event_type="discard",
            actor_id=actor.id,
            actor_email=actor.email,
        )
    )
    if rec.latest_revision_id == revision.id:
        result = await session.execute(
            select(MeetingTranscriptRevision)
            .where(
                MeetingTranscriptRevision.recording_id == rec.id,
                MeetingTranscriptRevision.state != "discarded",
                MeetingTranscriptRevision.id != revision.id,
            )
            .order_by(MeetingTranscriptRevision.revision_number.desc())
            .limit(1)
        )
        newest = result.scalar_one_or_none()
        rec.latest_revision_id = newest.id if newest else None
    await session.commit()
    await session.refresh(revision)
    return revision


async def record_edit_event(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    segment_id: uuid.UUID | None,
    segment_idx: int | None,
    event_type: str,
    before: dict | None,
    after: dict | None,
    actor: User,
    reason: str | None = None,
    request_id: str | None = None,
) -> None:
    """Append one immutable edit event (flushed with the caller's commit)."""
    session.add(
        MeetingTranscriptEditEvent(
            revision_id=revision_id,
            segment_id=segment_id,
            segment_idx=segment_idx,
            event_type=event_type,
            before=before,
            after=after,
            reason=reason,
            actor_id=actor.id,
            actor_email=actor.email,
            request_id=request_id,
        )
    )
    await session.flush()


def _s(segment: MeetingTranscriptSegment) -> str | None:
    return str(segment.speaker_profile_id) if segment.speaker_profile_id else None
