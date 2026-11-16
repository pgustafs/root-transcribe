"""The cleaned copy of a transcript.

``/workspaces/{id}/recordings/{id}/cleanup``: ask the language model to correct
what the speech model misheard (202, poll the GET). The result is a list of
corrected lines kept beside the transcript; the transcript itself is never
changed, and a person can dismiss any correction.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentUserDep, SessionDep
from app.api.v1.routes.meetings import (
    _load_recording,
    _require_cap,
    _require_sensitive_cap,
    ensure_meetings_enabled,
)
from app.api.v1.routes.workspaces import _load_authorized
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import request_id_ctx
from app.core.meeting_permissions import MeetingCapability
from app.crud import meeting_governance as meeting_governance_crud
from app.crud import task_outbox as task_outbox_crud
from app.crud import transcript_cleanup as transcript_cleanup_crud
from app.crud import transcript_revision as transcript_revision_crud
from app.models.transcript_cleanup import TranscriptCleanup
from app.schemas.cleanup import CleanupDecision, TranscriptCleanupPublic
from app.services.dependency_health import ensure_notes_path
from app.services.task_outbox import try_dispatch

router = APIRouter(tags=["cleanup"])

_CLEANUP = "/workspaces/{workspace_id}/recordings/{recording_id}/cleanup"


@router.get(_CLEANUP, dependencies=[Depends(ensure_meetings_enabled)])
async def latest_cleanup(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> TranscriptCleanupPublic | None:
    """The newest cleanup of the transcript version on display, or null (also
    the poll while one is being made)."""
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    rec = await _load_recording(session, workspace_id, recording_id)
    cleanup = await transcript_cleanup_crud.latest(
        session,
        recording_id=recording_id,
        revision_id=transcript_revision_crud.display_revision_id(rec),
    )
    return TranscriptCleanupPublic.model_validate(cleanup) if cleanup else None


@router.post(
    _CLEANUP,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ensure_meetings_enabled), Depends(ensure_notes_path)],
)
async def create_cleanup(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> TranscriptCleanupPublic:
    """Have the language model correct the transcript on display (202 + the
    pending cleanup). Making one again replaces the earlier one.

    The transcript text goes to the configured LLM, so restricted recordings
    need an explicit grant, as for notes.
    """
    await _load_authorized(session, user, workspace_id, level="content")
    rec = await _load_recording(session, workspace_id, recording_id)
    await _require_sensitive_cap(
        session, user, workspace_id, rec, MeetingCapability.CREATE_NOTES
    )
    revision_id = transcript_revision_crud.display_revision_id(rec)
    if rec.status != "completed" or revision_id is None:
        raise ConflictError("The recording has no finished transcript yet")
    current = await transcript_cleanup_crud.latest(
        session, recording_id=recording_id, revision_id=revision_id
    )
    if current is not None and current.status in ("pending", "processing"):
        return TranscriptCleanupPublic.model_validate(current)  # already working
    cleanup = TranscriptCleanup(
        recording_id=recording_id, revision_id=revision_id, created_by_id=user.id
    )
    session.add(cleanup)
    await session.flush()
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.clean_transcript",
        queue="celery",
        payload={"cleanup_id": str(cleanup.id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"cleanup:{cleanup.id}:send:1",
    )
    await meeting_governance_crud.record_audit(
        session,
        action="cleanup.create",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"cleanup_id": str(cleanup.id), "revision_id": str(revision_id)},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await try_dispatch(session, outbox)
    await session.refresh(cleanup)
    return TranscriptCleanupPublic.model_validate(cleanup)


async def _load_cleanup(
    session: SessionDep, recording_id: uuid.UUID, cleanup_id: uuid.UUID
) -> TranscriptCleanup:
    cleanup = await transcript_cleanup_crud.get(
        session, recording_id=recording_id, cleanup_id=cleanup_id
    )
    if cleanup is None:
        raise NotFoundError("Cleaned transcript not found")
    return cleanup


@router.post(
    _CLEANUP + "/{cleanup_id}/changes/{segment_id}",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def decide_change(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    cleanup_id: uuid.UUID,
    segment_id: uuid.UUID,
    payload: CleanupDecision,
    session: SessionDep,
    user: CurrentUserDep,
) -> TranscriptCleanupPublic:
    """Keep the original line instead of a correction (``dismiss``), or take
    the correction back (``use``). The transcript is not touched either way."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.CREATE_NOTES)
    await _load_recording(session, workspace_id, recording_id)
    cleanup = await _load_cleanup(session, recording_id, cleanup_id)
    wanted = "dismissed" if payload.decision == "dismiss" else "suggested"
    found = False
    changes = []
    for change in cleanup.changes:
        if change.get("segment_id") == str(segment_id):
            found = True
            change = {**change, "status": wanted}
        changes.append(change)
    if not found:
        raise NotFoundError("This line has no correction")
    # A new list: the JSON column only notices a replaced value.
    cleanup.changes = changes
    await session.commit()
    await session.refresh(cleanup)
    return TranscriptCleanupPublic.model_validate(cleanup)


@router.delete(
    _CLEANUP + "/{cleanup_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def delete_cleanup(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    cleanup_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> Response:
    """Remove a cleaned copy. The transcript is not affected."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.CREATE_NOTES)
    await _load_recording(session, workspace_id, recording_id)
    cleanup = await _load_cleanup(session, recording_id, cleanup_id)
    if cleanup.status in ("pending", "processing"):
        raise ConflictError("The cleaned transcript is still being made")
    await session.delete(cleanup)
    await meeting_governance_crud.record_audit(
        session,
        action="cleanup.delete",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"cleanup_id": str(cleanup_id)},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
