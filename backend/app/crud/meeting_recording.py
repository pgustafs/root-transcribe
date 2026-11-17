"""Meeting-recording repository.

Async functions back the API (create + read/poll + segment edits via the async
engine); the sync helpers drive the Celery worker (``session_scope``) as the
transcription pipeline progresses. Mirrors ``crud/ingest_job.py`` +
``crud/workspace_source.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.meeting_recording import MeetingRecording
from app.models.meeting_transcript_segment import MeetingTranscriptSegment
from app.models.speaker_profile import SpeakerProfile
from app.models.user import User

# --- async (API) ---


async def create(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    title: str,
    audio_key: str,
    audio_content_type: str,
    size_bytes: int,
    participant_consent: bool,
    actor: User,
    requested_language: str = "auto",
    expected_speakers: int | None = None,
    audio_layout: str = "mixed",
    mic_speakers: int | None = None,
    commit: bool = True,
) -> MeetingRecording:
    """Create a ``pending`` recording attributed to ``actor``.

    ``commit=False`` lets the route compose the recording with its attempt and
    outbox rows in ONE transaction (the transactional-enqueue guarantee, #6).
    """
    recording = MeetingRecording(
        workspace_id=workspace_id,
        title=title,
        status="pending",
        audio_key=audio_key,
        audio_content_type=audio_content_type,
        size_bytes=size_bytes,
        participant_consent=participant_consent,
        requested_language=requested_language,
        expected_speakers=expected_speakers,
        audio_layout=audio_layout,
        # Only an online-meeting recording has a microphone side to count.
        mic_speakers=mic_speakers if audio_layout == "mic_tab" else None,
        created_by_id=actor.id,
        created_by_email=actor.email,
    )
    session.add(recording)
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(recording)
    return recording


async def list_audio_keys_for_workspace(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """(recording_id, audio_key) for every not-yet-purged blob in a workspace —
    workspace deletion tombstones these so the CASCADE can't orphan them."""
    result = await session.execute(
        select(MeetingRecording.id, MeetingRecording.audio_key).where(
            MeetingRecording.workspace_id == workspace_id,
            MeetingRecording.audio_deleted.is_(False),
            MeetingRecording.audio_key.is_not(None),
        )
    )
    return [(rid, key) for rid, key in result.all()]


async def get_for_workspace(
    session: AsyncSession, *, workspace_id: uuid.UUID, recording_id: uuid.UUID
) -> MeetingRecording | None:
    """Fetch one recording, scoped to its workspace (tenancy guard)."""
    result = await session.execute(
        select(MeetingRecording).where(
            MeetingRecording.id == recording_id,
            MeetingRecording.workspace_id == workspace_id,
        )
    )
    return result.scalar_one_or_none()


async def lock(
    session: AsyncSession, recording_id: uuid.UUID
) -> MeetingRecording | None:
    """Lock the recording row for a review or lifecycle mutation.

    The caller holds the lock until transaction completion. ``populate_existing``
    refreshes any identity-map copy so a caller that waited for another writer
    sees the state committed by that writer.
    """
    result = await session.execute(
        select(MeetingRecording)
        .where(MeetingRecording.id == recording_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_for_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
    before: tuple[datetime, uuid.UUID] | None = None,
) -> Sequence[MeetingRecording]:
    """A page of a workspace's recordings, newest first (review finding: the
    implicit 100-cap silently hid older meetings with no way to page).

    ``before`` is a keyset cursor — the ``(created_at, id)`` of the previous
    page's last row (review round 4: mutable offsets duplicate a boundary row
    when a recording is inserted mid-pagination and silently skip one when an
    earlier row is deleted). ``offset`` remains for compat callers only.
    """
    stmt = (
        select(MeetingRecording)
        .where(MeetingRecording.workspace_id == workspace_id)
        .order_by(MeetingRecording.created_at.desc(), MeetingRecording.id.desc())
        .limit(limit)
    )
    if before is not None:
        created_at, row_id = before
        stmt = stmt.where(
            or_(
                MeetingRecording.created_at < created_at,
                and_(
                    MeetingRecording.created_at == created_at,
                    MeetingRecording.id < row_id,
                ),
            )
        )
    else:
        stmt = stmt.offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_segments(
    session: AsyncSession,
    *,
    recording_id: uuid.UUID,
    revision_id: uuid.UUID | None = None,
) -> Sequence[MeetingTranscriptSegment]:
    """One revision's transcript segments in order (issue #6: a recording's
    segments span every revision, so readers must scope to one). ``None`` keeps
    the legacy all-segments read for pre-revision callers/tests."""
    stmt = (
        select(MeetingTranscriptSegment)
        .where(MeetingTranscriptSegment.recording_id == recording_id)
        .order_by(MeetingTranscriptSegment.idx)
    )
    if revision_id is not None:
        stmt = stmt.where(MeetingTranscriptSegment.revision_id == revision_id)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_segment(
    session: AsyncSession, *, recording_id: uuid.UUID, segment_id: uuid.UUID
) -> MeetingTranscriptSegment | None:
    """One segment, scoped to its recording."""
    result = await session.execute(
        select(MeetingTranscriptSegment).where(
            MeetingTranscriptSegment.id == segment_id,
            MeetingTranscriptSegment.recording_id == recording_id,
        )
    )
    return result.scalar_one_or_none()


async def names_for_profiles(
    session: AsyncSession, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Map speaker-profile ids to display names (for segment serialization)."""
    wanted = [i for i in ids if i is not None]
    if not wanted:
        return {}
    result = await session.execute(
        select(SpeakerProfile.id, SpeakerProfile.display_name).where(
            SpeakerProfile.id.in_(wanted)
        )
    )
    return {row[0]: row[1] for row in result.all()}


def shown_speaker(
    segment: MeetingTranscriptSegment, profile_names: dict[uuid.UUID, str]
) -> str | None:
    """The name to show for a segment's speaker: the one a person typed, else
    the directory person, else None (the caller shows the detected label)."""
    if segment.speaker_display_name:
        return segment.speaker_display_name
    if segment.speaker_profile_id:
        return profile_names.get(segment.speaker_profile_id)
    return None


async def assign_speaker_by_label(
    session: AsyncSession,
    *,
    recording_id: uuid.UUID,
    revision_id: uuid.UUID,
    speaker_label: str,
    speaker_profile_id: uuid.UUID | None,
    edited_by_id: uuid.UUID | None,
    speaker_display_name: str | None = None,
) -> int:
    """Set the speaker on EVERY segment of one revision that carries
    ``speaker_label``: a directory person, a typed name, or neither (anonymous
    again). One replaces the other. Human-assigned, so any model confidence is
    cleared. Returns the number of segments updated."""
    result = await session.execute(
        update(MeetingTranscriptSegment)
        .where(
            MeetingTranscriptSegment.recording_id == recording_id,
            MeetingTranscriptSegment.revision_id == revision_id,
            MeetingTranscriptSegment.speaker_label == speaker_label,
        )
        .values(
            speaker_profile_id=speaker_profile_id,
            speaker_display_name=speaker_display_name,
            speaker_confidence=None,
            is_edited=True,
            edited_by_id=edited_by_id,
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def delete_recording(session: AsyncSession, recording: MeetingRecording) -> None:
    """Delete a recording (segments cascade)."""
    await session.delete(recording)
    await session.commit()


# --- sync (Celery worker) ---


def get_sync(session: Session, recording_id: uuid.UUID) -> MeetingRecording | None:
    """Fetch one recording by id (worker)."""
    return session.get(MeetingRecording, recording_id)


def mark_status_sync(
    session: Session,
    recording_id: uuid.UUID,
    *,
    status: str,
    step: str | None = None,
) -> None:
    """Move a recording to ``status`` and record the step in flight."""
    rec = session.get(MeetingRecording, recording_id)
    if rec is not None:
        rec.status = status
        rec.current_step = step
        rec.updated_at = datetime.now(UTC)
        session.commit()


def finish_sync(
    session: Session,
    recording_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    error_code: str | None = None,
    duration_seconds: float | None = None,
    language: str | None = None,
    commit: bool = True,
) -> None:
    """Mark a recording ``completed``/``failed`` with its outcome fields.

    ``error_code`` is the stable, safe classification (issue #37); ``error`` is
    the safe user-facing message. Both are cleared on a non-failed outcome.
    """
    rec = session.get(MeetingRecording, recording_id)
    if rec is None:
        return
    rec.status = status
    rec.current_step = None
    rec.error = error
    rec.error_code = error_code
    if duration_seconds is not None:
        rec.duration_seconds = duration_seconds
    if language is not None:
        rec.language = language
    rec.updated_at = datetime.now(UTC)
    if commit:
        session.commit()
