"""Meeting notes: agents that write documents from a transcript.

Two resources:

* ``/note-agents`` — the instructions. A shared list everyone can run (edited
  by admins and power users) plus personal agents only their owner sees.
* ``/workspaces/{id}/recordings/{id}/notes`` — the documents. Creating one
  queues an agent run (202, poll the list); the result is Markdown that can be
  edited, restored and exported.

The agent runtime itself is the separate agent-runner container.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import PRIVILEGED_ROLES, CurrentUserDep, SessionDep
from app.api.v1.routes.meetings import (
    _load_recording,
    _require_cap,
    _require_sensitive_cap,
    ensure_meetings_enabled,
)
from app.api.v1.routes.workspaces import _load_authorized
from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.logging import request_id_ctx
from app.core.meeting_permissions import MeetingCapability
from app.crud import meeting_governance as meeting_governance_crud
from app.crud import meeting_note as meeting_note_crud
from app.crud import meeting_recording as meeting_recording_crud
from app.crud import speaker_name_run as speaker_name_run_crud
from app.crud import task_outbox as task_outbox_crud
from app.crud import transcript_revision as transcript_revision_crud
from app.models.meeting_note import MeetingNote, NoteAgent
from app.models.speaker_name_run import SpeakerNameRun
from app.models.user import User
from app.schemas.note import (
    MeetingNoteCreate,
    MeetingNotePublic,
    MeetingNoteUpdate,
    NoteAgentCreate,
    NoteAgentPublic,
    NoteAgentUpdate,
    SpeakerNameDecision,
    SpeakerNameRunPublic,
)
from app.services import note_export
from app.services.dependency_health import ensure_notes_path
from app.services.note_defaults import BUILTIN_BY_KEY
from app.services.task_outbox import try_dispatch

router = APIRouter(tags=["notes"])

_RECORDING = "/workspaces/{workspace_id}/recordings/{recording_id}"

# --- agents ------------------------------------------------------------------


def _can_edit(agent: NoteAgent, user: User) -> bool:
    """Shared agents: admins and power users. Personal agents: their owner."""
    if agent.owner_id is None:
        return user.role in PRIVILEGED_ROLES
    return agent.owner_id == user.id


def _agent_public(agent: NoteAgent, user: User) -> NoteAgentPublic:
    builtin = BUILTIN_BY_KEY.get(agent.builtin_key or "")
    return NoteAgentPublic(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        instructions=agent.instructions,
        is_active=agent.is_active,
        shared=agent.owner_id is None,
        builtin=agent.builtin_key is not None,
        modified=builtin is not None
        and (agent.instructions != builtin.instructions or agent.name != builtin.name),
        can_edit=_can_edit(agent, user),
        updated_at=agent.updated_at,
    )


async def _editable_agent(
    session: SessionDep, user: User, agent_id: uuid.UUID
) -> NoteAgent:
    agent = await meeting_note_crud.get_agent_for(session, user, agent_id)
    if agent is None:
        raise NotFoundError("Agent not found")
    if not _can_edit(agent, user):
        raise PermissionDeniedError(
            "Only administrators and power users can change shared agents"
        )
    return agent


@router.get("/note-agents")
async def list_note_agents(
    session: SessionDep, user: CurrentUserDep
) -> list[NoteAgentPublic]:
    """Shared agents and the caller's own."""
    agents = await meeting_note_crud.list_agents_for(session, user)
    return [_agent_public(agent, user) for agent in agents]


@router.post("/note-agents", status_code=status.HTTP_201_CREATED)
async def create_note_agent(
    payload: NoteAgentCreate, session: SessionDep, user: CurrentUserDep
) -> NoteAgentPublic:
    """Add a personal agent, or (admins and power users) a shared one."""
    if payload.shared and user.role not in PRIVILEGED_ROLES:
        raise PermissionDeniedError(
            "Only administrators and power users can add shared agents"
        )
    agent = await meeting_note_crud.create_agent(
        session,
        actor=user,
        shared=payload.shared,
        name=payload.name.strip(),
        description=(payload.description or "").strip() or None,
        instructions=payload.instructions.strip(),
    )
    await session.commit()
    await session.refresh(agent)
    return _agent_public(agent, user)


@router.patch("/note-agents/{agent_id}")
async def update_note_agent(
    agent_id: uuid.UUID,
    payload: NoteAgentUpdate,
    session: SessionDep,
    user: CurrentUserDep,
) -> NoteAgentPublic:
    """Change an agent. Takes effect on the next run; existing notes keep the
    instructions they were written with."""
    agent = await _editable_agent(session, user, agent_id)
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        agent.name = fields["name"].strip()
    if "description" in fields:
        agent.description = (fields["description"] or "").strip() or None
    if "instructions" in fields and fields["instructions"] is not None:
        agent.instructions = fields["instructions"].strip()
    if "is_active" in fields and fields["is_active"] is not None:
        agent.is_active = fields["is_active"]
    await session.commit()
    await session.refresh(agent)
    return _agent_public(agent, user)


@router.post("/note-agents/{agent_id}/reset")
async def reset_note_agent(
    agent_id: uuid.UUID, session: SessionDep, user: CurrentUserDep
) -> NoteAgentPublic:
    """Put a built-in agent back to the text shipped with the app."""
    agent = await _editable_agent(session, user, agent_id)
    builtin = BUILTIN_BY_KEY.get(agent.builtin_key or "")
    if builtin is None:
        raise ConflictError("Only built-in agents have a default to return to")
    agent.name = builtin.name
    agent.description = builtin.description
    agent.instructions = builtin.instructions
    await session.commit()
    await session.refresh(agent)
    return _agent_public(agent, user)


@router.delete("/note-agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note_agent(
    agent_id: uuid.UUID, session: SessionDep, user: CurrentUserDep
) -> None:
    """Remove an agent. Notes it wrote stay. Built-ins can only be switched off."""
    agent = await _editable_agent(session, user, agent_id)
    if agent.builtin_key is not None:
        raise ConflictError("Built-in agents cannot be deleted; switch them off")
    await session.delete(agent)
    await session.commit()


# --- notes -------------------------------------------------------------------


def _note_public(note: MeetingNote) -> MeetingNotePublic:
    public = MeetingNotePublic.model_validate(note)
    public.edited = note.edited_at is not None
    return public


async def _load_note(
    session: SessionDep, recording_id: uuid.UUID, note_id: uuid.UUID
) -> MeetingNote:
    note = await meeting_note_crud.get_for_recording(
        session, recording_id=recording_id, note_id=note_id
    )
    if note is None:
        raise NotFoundError("Note not found")
    return note


@router.get(_RECORDING + "/notes", dependencies=[Depends(ensure_meetings_enabled)])
async def list_notes(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[MeetingNotePublic]:
    """Notes on a recording, newest first (also the poll for running ones)."""
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    await _load_recording(session, workspace_id, recording_id)
    notes = await meeting_note_crud.list_for_recording(
        session, recording_id=recording_id
    )
    return [_note_public(note) for note in notes]


@router.post(
    _RECORDING + "/notes",
    dependencies=[Depends(ensure_meetings_enabled), Depends(ensure_notes_path)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_note(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    payload: MeetingNoteCreate,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingNotePublic:
    """Run an agent on the recording's transcript (202 + the pending note).

    The transcript text goes to the configured LLM, so restricted recordings
    need the same explicit grant as an export.
    """
    await _load_authorized(session, user, workspace_id, level="content")
    rec = await _load_recording(session, workspace_id, recording_id)
    await _require_sensitive_cap(
        session, user, workspace_id, rec, MeetingCapability.CREATE_NOTES
    )
    agent = await meeting_note_crud.get_agent_for(session, user, payload.agent_id)
    if agent is None:
        raise NotFoundError("Agent not found")
    if not agent.is_active:
        raise ConflictError("This agent is switched off")
    revision_id = transcript_revision_crud.display_revision_id(rec)
    if rec.status != "completed" or revision_id is None:
        raise ConflictError("The recording has no finished transcript yet")
    note = await meeting_note_crud.create_pending(
        session,
        workspace_id=workspace_id,
        recording_id=recording_id,
        revision_id=revision_id,
        agent=agent,
        actor=user,
    )
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.generate_meeting_note",
        queue="celery",
        payload={"note_id": str(note.id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"note:{note.id}:send:1",
    )
    await meeting_governance_crud.record_audit(
        session,
        action="note.create",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={
            "note_id": str(note.id),
            "agent_id": str(agent.id),
            "agent": agent.name,
            "revision_id": str(revision_id),
        },
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await try_dispatch(session, outbox)
    await session.refresh(note)
    return _note_public(note)


@router.patch(
    _RECORDING + "/notes/{note_id}", dependencies=[Depends(ensure_meetings_enabled)]
)
async def update_note(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    note_id: uuid.UUID,
    payload: MeetingNoteUpdate,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingNotePublic:
    """Save an edited note. ``expected_version`` guards against overwriting
    someone else's edit (409)."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.CREATE_NOTES)
    await _load_recording(session, workspace_id, recording_id)
    note = await _load_note(session, recording_id, note_id)
    if note.status != "ready":
        raise ConflictError("Only a finished note can be edited")
    if note.version != payload.expected_version:
        raise ConflictError(
            "Someone else changed this note; reload it before saving again"
        )
    meeting_note_crud.apply_edit(note, content=payload.content_markdown, actor=user)
    await session.commit()
    await session.refresh(note)
    return _note_public(note)


@router.post(
    _RECORDING + "/notes/{note_id}/restore",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def restore_note(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingNotePublic:
    """Discard edits: back to the text the agent wrote."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.CREATE_NOTES)
    await _load_recording(session, workspace_id, recording_id)
    note = await _load_note(session, recording_id, note_id)
    if note.status != "ready":
        raise ConflictError("Only a finished note can be restored")
    meeting_note_crud.restore_generated(note)
    await session.commit()
    await session.refresh(note)
    return _note_public(note)


@router.delete(
    _RECORDING + "/notes/{note_id}",
    dependencies=[Depends(ensure_meetings_enabled)],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_note(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.CREATE_NOTES)
    await _load_recording(session, workspace_id, recording_id)
    note = await _load_note(session, recording_id, note_id)
    await meeting_governance_crud.record_audit(
        session,
        action="note.delete",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"note_id": str(note.id), "agent": note.agent_name},
        request_id=request_id_ctx.get(),
    )
    await session.delete(note)
    await session.commit()


@router.get(
    _RECORDING + "/notes/{note_id}/export",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def export_note(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    format: Annotated[str, Query()] = "md",
) -> Response:
    """Download a note as md | docx | pdf (same gate as a transcript export)."""
    await _load_authorized(session, user, workspace_id, level="read")
    rec = await _load_recording(session, workspace_id, recording_id)
    await _require_sensitive_cap(
        session, user, workspace_id, rec, MeetingCapability.EXPORT
    )
    if format not in note_export.EXPORT_FORMATS:
        raise AppError(
            "Unsupported export format; use one of: "
            + ", ".join(note_export.EXPORT_FORMATS),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    note = await _load_note(session, recording_id, note_id)
    if note.status != "ready" or not note.content_markdown:
        raise ConflictError("This note has nothing to export yet")
    title = f"{rec.title} - {note.agent_name}"
    markdown = note.content_markdown
    body = await anyio.to_thread.run_sync(
        lambda: note_export.render(format, title=title, markdown=markdown)
    )
    await meeting_governance_crud.record_audit(
        session,
        action="note.export",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"format": format, "note_id": str(note.id)},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    safe_name = (
        "".join(ch for ch in title if ch.isascii() and (ch.isalnum() or ch in " -_"))
        .strip()
        .replace("  ", " ")
        or "note"
    )
    return Response(
        content=body,
        media_type=note_export.CONTENT_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.{format}"'},
    )


# --- speaker names found in the transcript ----------------------------------


@router.get(
    _RECORDING + "/speaker-names", dependencies=[Depends(ensure_meetings_enabled)]
)
async def latest_speaker_name_run(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> SpeakerNameRunPublic | None:
    """The newest name search on the current transcript, or null."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.RELABEL_SPEAKERS)
    rec = await _load_recording(session, workspace_id, recording_id)
    run = await speaker_name_run_crud.latest(
        session,
        recording_id=recording_id,
        revision_id=transcript_revision_crud.display_revision_id(rec),
    )
    return SpeakerNameRunPublic.model_validate(run) if run else None


@router.post(
    _RECORDING + "/speaker-names/identify",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ensure_meetings_enabled), Depends(ensure_notes_path)],
)
async def identify_speaker_names(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> SpeakerNameRunPublic:
    """Ask the language model to find the speakers' names in what was said
    (202 + the pending run; poll ``GET …/speaker-names``).

    The transcript text goes to the configured LLM, so restricted recordings
    need an explicit grant, as for notes. The run only proposes names.
    """
    await _load_authorized(session, user, workspace_id, level="content")
    rec = await _load_recording(session, workspace_id, recording_id)
    await _require_sensitive_cap(
        session, user, workspace_id, rec, MeetingCapability.RELABEL_SPEAKERS
    )
    revision_id = transcript_revision_crud.display_revision_id(rec)
    if rec.status != "completed" or revision_id is None:
        raise ConflictError("The recording has no finished transcript yet")
    current = await speaker_name_run_crud.latest(
        session, recording_id=recording_id, revision_id=revision_id
    )
    if current is not None and current.status in ("pending", "processing"):
        return SpeakerNameRunPublic.model_validate(current)  # already working
    run = SpeakerNameRun(
        recording_id=recording_id, revision_id=revision_id, created_by_id=user.id
    )
    session.add(run)
    await session.flush()
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.identify_speaker_names",
        queue="celery",
        payload={"run_id": str(run.id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"speaker-names:{run.id}:send:1",
    )
    await meeting_governance_crud.record_audit(
        session,
        action="speaker_names.identify",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"run_id": str(run.id), "revision_id": str(revision_id)},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await try_dispatch(session, outbox)
    await session.refresh(run)
    return SpeakerNameRunPublic.model_validate(run)


@router.post(
    _RECORDING + "/speaker-names/{run_id}/suggestions/{index}",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def decide_speaker_name(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    run_id: uuid.UUID,
    index: int,
    payload: SpeakerNameDecision,
    session: SessionDep,
    user: CurrentUserDep,
) -> SpeakerNameRunPublic:
    """Use a suggested name for the speaker (every turn of that voice), or
    dismiss it. Accepting is a human decision and is recorded as one."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.RELABEL_SPEAKERS)
    await _load_recording(session, workspace_id, recording_id)
    run = await speaker_name_run_crud.get(
        session, recording_id=recording_id, run_id=run_id
    )
    if run is None or not 0 <= index < len(run.suggestions):
        raise NotFoundError("Suggestion not found")
    suggestion = dict(run.suggestions[index])
    if suggestion["status"] != "pending":
        raise ConflictError("This suggestion was already decided")
    if payload.decision == "accept":
        updated = await meeting_recording_crud.assign_speaker_by_label(
            session,
            recording_id=recording_id,
            revision_id=run.revision_id,
            speaker_label=suggestion["speaker_label"],
            speaker_profile_id=None,
            speaker_display_name=suggestion["name"],
            edited_by_id=user.id,
        )
        await transcript_revision_crud.record_edit_event(
            session,
            revision_id=run.revision_id,
            segment_id=None,
            segment_idx=None,
            event_type="bulk_assign",
            before={"speaker_label": suggestion["speaker_label"]},
            after={
                "speaker_name": suggestion["name"],
                "suggested_from": suggestion["quote"],
                "updated_segments": updated,
            },
            actor=user,
            request_id=request_id_ctx.get(),
        )
    suggestion["status"] = "accepted" if payload.decision == "accept" else "dismissed"
    # A new list: the JSON column only notices a replaced value.
    run.suggestions = [
        suggestion if i == index else item for i, item in enumerate(run.suggestions)
    ]
    await session.commit()
    await session.refresh(run)
    return SpeakerNameRunPublic.model_validate(run)
