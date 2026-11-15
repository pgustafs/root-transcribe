"""Study notes: notebooks of screenshots with transcribed excerpts.

A learner records a browser tab and presses "Take note" (in the app or with a
keyboard shortcut on their computer). Each press stores the visible frame and
a short piece of the tab's audio, which is transcribed on its own.

* ``/workspaces/{id}/study/…`` — notebooks, sessions and notes. Private to
  their owner; needs the ``study_notes`` capability.
* ``/study/hotkey…`` — the shortcut credential and the press itself.
* ``/study/commands/…`` — the stream the recording browser listens on, and its
  acknowledgement of a press.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated

import anyio
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.api.deps import CurrentUserDep
from app.api.v1.routes.meetings import (
    _require_cap,
    _require_sensitive_cap,
    ensure_meetings_enabled,
)
from app.api.v1.routes.workspaces import _load_authorized
from app.core.config import settings
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.logging import request_id_ctx
from app.core.meeting_permissions import MeetingCapability
from app.core.rate_limit import get_rate_limiter
from app.crud import audio_object as audio_object_crud
from app.crud import meeting_governance as meeting_governance_crud
from app.crud import meeting_recording as meeting_recording_crud
from app.crud import study as study_crud
from app.crud import task_outbox as task_outbox_crud
from app.db.session import SessionDep
from app.models.study import StudyNote, StudyNotebook, StudySession
from app.models.user import User
from app.schemas.study import (
    CommandAck,
    HotkeyCredentialPublic,
    NotebookCreate,
    NotebookDetail,
    NotebookPublic,
    NotebookUpdate,
    NotePublic,
    NoteUpdate,
    SessionCreate,
    SessionPublic,
    SessionUpdate,
)
from app.services import study_commands, study_export, study_source
from app.services.audio_store import get_audio_store, key_namespace
from app.services.task_outbox import try_dispatch

router = APIRouter(tags=["study"])

_WS = "/workspaces/{workspace_id}/study"
_ENABLED = [Depends(ensure_meetings_enabled)]

MAX_IMAGE_BYTES = 25 * 1024 * 1024
#: 16 kHz mono 16-bit is about 1.9 MB a minute; the STT endpoint takes 25 MB.
MAX_AUDIO_BYTES = 24 * 1024 * 1024
HOTKEY_PER_MINUTE = 30
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: A command older than this cannot capture the moment the key was pressed.
STALE_COMMAND_MS = 2000

# --- access -------------------------------------------------------------------


async def _guard(session: SessionDep, user: User, workspace_id: uuid.UUID) -> None:
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.STUDY_NOTES)


async def _notebook(
    session: SessionDep, user: User, workspace_id: uuid.UUID, notebook_id: uuid.UUID
) -> StudyNotebook:
    """The caller's own notebook. Someone else's does not exist for them."""
    notebook = await study_crud.get_notebook(
        session, notebook_id=notebook_id, workspace_id=workspace_id, owner_id=user.id
    )
    if notebook is None:
        raise NotFoundError("Notebook not found")
    return notebook


async def _session(
    session: SessionDep, user: User, workspace_id: uuid.UUID, session_id: uuid.UUID
) -> tuple[StudySession, StudyNotebook]:
    found = await study_crud.owned_session(
        session, session_id=session_id, owner_id=user.id
    )
    if found is None or found[1].workspace_id != workspace_id:
        raise NotFoundError("Study session not found")
    return found


async def _note(
    session: SessionDep, user: User, workspace_id: uuid.UUID, note_id: uuid.UUID
) -> tuple[StudyNote, StudyNotebook]:
    note = await study_crud.get_note(session, note_id)
    if note is None:
        raise NotFoundError("Note not found")
    notebook = await _notebook(session, user, workspace_id, note.notebook_id)
    return note, notebook


def _note_public(note: StudyNote) -> NotePublic:
    public = NotePublic.model_validate(note)
    public.has_image = bool(note.image_key)
    return public


def _notebook_public(notebook: StudyNotebook, count: int) -> NotebookPublic:
    public = NotebookPublic.model_validate(notebook)
    public.note_count = count
    return public


def _source(url: str | None) -> tuple[str | None, str | None]:
    if url is None:
        return None, None
    if not study_source.is_http_url(url):
        raise AppError("The source must be a web link", status_code=422)
    return study_source.normalized(url), study_source.source_key(url)


# --- notebooks ----------------------------------------------------------------


@router.get(f"{_WS}/notebooks", dependencies=_ENABLED)
async def list_notebooks(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    recording_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[NotebookPublic]:
    """The caller's notebooks here (optionally: those with notes from one
    recording)."""
    await _guard(session, user, workspace_id)
    notebooks = list(
        await study_crud.list_notebooks(
            session, workspace_id=workspace_id, owner_id=user.id
        )
    )
    if recording_id is not None:
        wanted = {
            s.notebook_id
            for s in await study_crud.sessions_for_recording(session, recording_id)
        }
        notebooks = [n for n in notebooks if n.id in wanted]
    counts = await study_crud.note_counts(session, [n.id for n in notebooks])
    return [_notebook_public(n, counts.get(n.id, 0)) for n in notebooks]


@router.post(f"{_WS}/notebooks", dependencies=_ENABLED)
async def create_notebook(
    workspace_id: uuid.UUID,
    payload: NotebookCreate,
    session: SessionDep,
    user: CurrentUserDep,
    response: Response,
) -> NotebookPublic:
    """Create a notebook, or return the one this source already has (200)."""
    await _guard(session, user, workspace_id)
    url, key = _source(payload.source_url)
    notebook, created = await study_crud.create_notebook(
        session,
        workspace_id=workspace_id,
        owner_id=user.id,
        title=payload.title,
        source_url=url,
        source_key=key,
    )
    await session.commit()
    await session.refresh(notebook)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    counts = await study_crud.note_counts(session, [notebook.id])
    return _notebook_public(notebook, counts.get(notebook.id, 0))


@router.get(f"{_WS}/notebooks/{{notebook_id}}", dependencies=_ENABLED)
async def get_notebook(
    workspace_id: uuid.UUID,
    notebook_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> NotebookDetail:
    await _guard(session, user, workspace_id)
    notebook = await _notebook(session, user, workspace_id, notebook_id)
    notes = await study_crud.list_notes(session, notebook.id)
    detail = NotebookDetail.model_validate(notebook)
    detail.note_count = len(notes)
    detail.sessions = [
        SessionPublic.model_validate(s)
        for s in await study_crud.list_sessions(session, notebook.id)
    ]
    detail.notes = [_note_public(n) for n in notes]
    return detail


@router.patch(f"{_WS}/notebooks/{{notebook_id}}", dependencies=_ENABLED)
async def update_notebook(
    workspace_id: uuid.UUID,
    notebook_id: uuid.UUID,
    payload: NotebookUpdate,
    session: SessionDep,
    user: CurrentUserDep,
) -> NotebookPublic:
    await _guard(session, user, workspace_id)
    notebook = await _notebook(session, user, workspace_id, notebook_id)
    if payload.title is not None:
        notebook.title = payload.title
    if payload.source_url is not None:
        url, key = _source(payload.source_url)
        if key and key != notebook.source_key:
            other = await study_crud.get_notebook_by_source(
                session, workspace_id=workspace_id, owner_id=user.id, source_key=key
            )
            if other is not None and other.id != notebook.id:
                raise ConflictError("Another notebook already has this source")
        notebook.source_url, notebook.source_key = url, key
    await session.commit()
    await session.refresh(notebook)
    counts = await study_crud.note_counts(session, [notebook.id])
    return _notebook_public(notebook, counts.get(notebook.id, 0))


@router.delete(
    f"{_WS}/notebooks/{{notebook_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_ENABLED,
)
async def delete_notebook(
    workspace_id: uuid.UUID,
    notebook_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    await _guard(session, user, workspace_id)
    notebook = await _notebook(session, user, workspace_id, notebook_id)
    notes = await study_crud.list_notes(session, notebook.id)
    await study_crud.tombstone_media(
        session,
        notes,
        workspace_id=workspace_id,
        reason="notebook_deleted",
        actor=user,
    )
    await meeting_governance_crud.record_audit(
        session,
        action="study.notebook.delete",
        actor=user,
        workspace_id=workspace_id,
        target={"notebook_id": str(notebook.id), "notes": len(notes)},
        request_id=request_id_ctx.get(),
    )
    await session.delete(notebook)
    await session.commit()


@router.get(f"{_WS}/notebooks/{{notebook_id}}/export", dependencies=_ENABLED)
async def export_notebook(
    workspace_id: uuid.UUID,
    notebook_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> Response:
    """The notebook as a ZIP: one Markdown file and its images."""
    await _guard(session, user, workspace_id)
    notebook = await _notebook(session, user, workspace_id, notebook_id)
    sessions = {s.id: s for s in await study_crud.list_sessions(session, notebook.id)}
    # A notebook can span recordings; each one's restriction applies to its
    # entries, so one restricted recording without a grant refuses the export.
    for recording_id in {s.recording_id for s in sessions.values() if s.recording_id}:
        rec = await meeting_recording_crud.get_for_workspace(
            session, workspace_id=workspace_id, recording_id=recording_id
        )
        if rec is not None:
            await _require_sensitive_cap(
                session, user, workspace_id, rec, MeetingCapability.STUDY_NOTES
            )
    notes = await study_crud.list_notes(session, notebook.id)
    title, source_url = notebook.title, notebook.source_url
    rows = [
        (
            note.image_key,
            study_export.ExportNote(
                session_id=str(note.session_id),
                session_started=sessions[note.session_id].started_at,
                seq=note.seq,
                captured_ms=note.captured_ms,
                audio_start_ms=note.audio_start_ms,
                audio_end_ms=note.audio_end_ms,
                status=note.status,
                text=note.text,
                image=None,
                source_time_url=note.source_time_url,
            ),
        )
        for note in notes
    ]
    store = get_audio_store()

    def _build() -> bytes:
        from dataclasses import replace

        entries = []
        for image_key, entry in rows:
            if image_key:
                with contextlib.suppress(Exception):  # text survives a lost image
                    entry = replace(entry, image=store.get_sync(image_key))
            entries.append(entry)
        return study_export.build_zip(title=title, source_url=source_url, notes=entries)

    body = await anyio.to_thread.run_sync(_build)
    await meeting_governance_crud.record_audit(
        session,
        action="study.notebook.export",
        actor=user,
        workspace_id=workspace_id,
        target={"notebook_id": str(notebook_id), "notes": len(rows)},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    filename = f"{study_export.slug(title)}.zip"
    ascii_name = filename.encode("ascii", "ignore").decode().strip("-.")
    if not ascii_name.endswith("zip") or len(ascii_name) < 5:
        ascii_name = "study-notes.zip"
    return Response(
        content=body,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{ascii_name}"'},
    )


# --- sessions -----------------------------------------------------------------


@router.put(f"{_WS}/sessions/{{session_id}}", dependencies=_ENABLED)
async def put_session(
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: SessionCreate,
    session: SessionDep,
    user: CurrentUserDep,
) -> SessionPublic:
    """Start a study session. Repeating the request returns the same session."""
    await _guard(session, user, workspace_id)
    await _require_cap(session, user, workspace_id, MeetingCapability.RECORD)
    notebook = await _notebook(session, user, workspace_id, payload.notebook_id)
    existing = await study_crud.get_session(session, session_id)
    if existing is not None:
        if existing.notebook_id != notebook.id:
            raise ConflictError("This session belongs to another notebook")
        return SessionPublic.model_validate(existing)
    row = StudySession(
        id=session_id,
        notebook_id=notebook.id,
        language=payload.language,
        mode=payload.mode,
        started_at=payload.started_at or datetime.now(UTC),
    )
    session.add(row)
    await study_crud.touch_notebook(session, notebook.id)
    await session.commit()
    await session.refresh(row)
    return SessionPublic.model_validate(row)


@router.patch(f"{_WS}/sessions/{{session_id}}", dependencies=_ENABLED)
async def link_session(
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: SessionUpdate,
    session: SessionDep,
    user: CurrentUserDep,
) -> SessionPublic:
    """Tie the session to its uploaded recording, so the recording's
    restrictions and deletion apply to the notes."""
    await _guard(session, user, workspace_id)
    row, _owner_notebook = await _session(session, user, workspace_id, session_id)
    rec = await meeting_recording_crud.get_for_workspace(
        session, workspace_id=workspace_id, recording_id=payload.recording_id
    )
    if rec is None or rec.created_by_id != user.id:
        raise NotFoundError("Recording not found")
    row.recording_id = rec.id
    await session.commit()
    await session.refresh(row)
    return SessionPublic.model_validate(row)


@router.delete(
    f"{_WS}/sessions/{{session_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_ENABLED,
)
async def delete_session(
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    """Discard a session and its notes (the recording was cancelled)."""
    await _guard(session, user, workspace_id)
    found = await study_crud.owned_session(
        session, session_id=session_id, owner_id=user.id
    )
    if found is None or found[1].workspace_id != workspace_id:
        return  # already gone: the discard is repeatable
    notes = await study_crud.notes_for_sessions(session, [session_id])
    await study_crud.tombstone_media(
        session, notes, workspace_id=workspace_id, reason="user_request", actor=user
    )
    await study_crud.delete_sessions(session, [session_id])
    await session.commit()


# --- notes --------------------------------------------------------------------


async def _queue_transcription(session: SessionDep, note: StudyNote, user: User):
    note.send_count = (note.send_count or 0) + 1
    note.status = "pending"
    note.error = None
    return await task_outbox_crud.enqueue(
        session,
        task_name="tasks.transcribe_study_note",
        queue="celery",
        payload={"note_id": str(note.id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"study-note:{note.id}:send:{note.send_count}",
    )


async def _read_limited(upload: UploadFile, limit: int, what: str) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise AppError(f"The {what} is too large", status_code=413)
    return data


def _is_wav(data: bytes) -> bool:
    return len(data) > 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _png_size(data: bytes) -> tuple[int | None, int | None]:
    """Width and height from the PNG header (IHDR is always the first chunk)."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None, None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


@router.put(f"{_WS}/notes/{{note_id}}", dependencies=_ENABLED)
async def put_note(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    response: Response,
    session_id: Annotated[uuid.UUID, Form()],
    seq: Annotated[int, Form(ge=0)],
    captured_ms: Annotated[int, Form(ge=0)],
    image: Annotated[UploadFile, File()],
    audio: Annotated[UploadFile | None, File()] = None,
    audio_start_ms: Annotated[int | None, Form(ge=0)] = None,
    audio_end_ms: Annotated[int | None, Form(ge=0)] = None,
    end_reason: Annotated[str | None, Form(max_length=30)] = None,
) -> NotePublic:
    """Store one capture. The id comes from the browser, so sending the same
    note again changes nothing and returns what is stored."""
    await _guard(session, user, workspace_id)
    await _require_cap(session, user, workspace_id, MeetingCapability.RECORD)
    study_session, notebook = await _session(session, user, workspace_id, session_id)
    existing = await study_crud.get_note(session, note_id)
    if existing is not None:
        if existing.session_id != study_session.id:
            raise ConflictError("This note belongs to another session")
        return _note_public(existing)

    image_bytes = await _read_limited(image, MAX_IMAGE_BYTES, "image")
    if not image_bytes.startswith(_PNG_MAGIC):
        raise AppError("The image must be a PNG file", status_code=422)
    audio_bytes: bytes | None = None
    if audio is not None:
        audio_bytes = await _read_limited(audio, MAX_AUDIO_BYTES, "audio")
        if not _is_wav(audio_bytes):
            raise AppError("The audio must be a WAV file", status_code=422)
        if audio_start_ms is None or audio_end_ms is None:
            raise AppError("Audio needs its start and end time", status_code=422)

    store = get_audio_store()
    # Keys are derived from the note id: a repeated upload overwrites itself.
    base = f"{key_namespace()}workspaces/{workspace_id}/study/{note_id}"
    image_key = f"{base}.png"
    audio_key = f"{base}.wav" if audio_bytes is not None else None
    notebook_id, study_session_id = notebook.id, study_session.id
    try:
        await anyio.to_thread.run_sync(store.put, image_key, image_bytes, "image/png")
        if audio_bytes is not None:
            await anyio.to_thread.run_sync(
                store.put, audio_key, audio_bytes, "audio/wav"
            )
    except Exception as exc:
        raise AppError(
            "The note could not be stored; it will be sent again",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    for key, kind in ((image_key, "study_image"), (audio_key, "study_audio")):
        if key:
            await audio_object_crud.ensure_for_key(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=key,
                kind=kind,
                ref_id=note_id,
                workspace_id=workspace_id,
            )

    width, height = _png_size(image_bytes)
    note = StudyNote(
        id=note_id,
        notebook_id=notebook_id,
        session_id=study_session_id,
        seq=seq,
        captured_ms=captured_ms,
        audio_start_ms=audio_start_ms if audio_bytes is not None else None,
        audio_end_ms=audio_end_ms if audio_bytes is not None else None,
        image_key=image_key,
        image_width=width,
        image_height=height,
        audio_key=audio_key,
        has_audio=audio_bytes is not None,
        status="pending" if audio_bytes is not None else "screenshot_only",
        end_reason=end_reason,
        send_count=0,
    )
    session.add(note)
    outbox = None
    if audio_bytes is not None:
        outbox = await _queue_transcription(session, note, user)
    await study_crud.touch_notebook(session, notebook_id)
    await session.commit()
    if outbox is not None:
        await try_dispatch(session, outbox)
    await session.refresh(note)
    response.status_code = status.HTTP_201_CREATED
    return _note_public(note)


@router.post(f"{_WS}/notes/{{note_id}}/retry", dependencies=_ENABLED)
async def retry_note(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> NotePublic:
    """Transcribe a failed note again (its audio is kept until it succeeds)."""
    await _guard(session, user, workspace_id)
    note, _owner_notebook = await _note(session, user, workspace_id, note_id)
    if note.status != "failed":
        return _note_public(note)
    if not note.audio_key:
        raise ConflictError("The audio of this note is no longer stored")
    outbox = await _queue_transcription(session, note, user)
    await session.commit()
    await try_dispatch(session, outbox)
    await session.refresh(note)
    return _note_public(note)


@router.patch(f"{_WS}/notes/{{note_id}}", dependencies=_ENABLED)
async def update_note(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    payload: NoteUpdate,
    session: SessionDep,
    user: CurrentUserDep,
) -> NotePublic:
    """Attach the player's "copy URL at current time" link to a note. It must
    point at the notebook's own source."""
    await _guard(session, user, workspace_id)
    note, notebook = await _note(session, user, workspace_id, note_id)
    url = payload.source_time_url
    if not url:
        note.source_time_url = None
    else:
        if not study_source.is_http_url(url):
            raise AppError("The link must be a web link", status_code=422)
        if notebook.source_key is None or (
            study_source.source_key(url) != notebook.source_key
        ):
            raise AppError(
                "The link does not point at this notebook's source", status_code=422
            )
        note.source_time_url = study_source.normalized(url)
    await session.commit()
    await session.refresh(note)
    return _note_public(note)


@router.delete(
    f"{_WS}/notes/{{note_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_ENABLED,
)
async def delete_note(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    await _guard(session, user, workspace_id)
    note, _owner_notebook = await _note(session, user, workspace_id, note_id)
    await study_crud.tombstone_media(
        session, [note], workspace_id=workspace_id, reason="user_request", actor=user
    )
    await session.delete(note)
    await session.commit()


@router.get(f"{_WS}/notes/{{note_id}}/image", dependencies=_ENABLED)
async def note_image(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> Response:
    await _guard(session, user, workspace_id)
    note, _owner_notebook = await _note(session, user, workspace_id, note_id)
    if not note.image_key:
        raise NotFoundError("This note has no image")
    data = await get_audio_store().get(note.image_key)
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# --- shortcut credential ------------------------------------------------------


@router.get("/study/hotkey/credential")
async def get_hotkey_credential(
    session: SessionDep, user: CurrentUserDep
) -> HotkeyCredentialPublic:
    credential = await study_crud.get_credential(session, user.id)
    if credential is None:
        return HotkeyCredentialPublic(exists=False)
    return HotkeyCredentialPublic(
        exists=True,
        hint=credential.hint,
        created_at=credential.created_at,
        last_used_at=credential.last_used_at,
    )


@router.post("/study/hotkey/credential", status_code=status.HTTP_201_CREATED)
async def create_hotkey_credential(
    session: SessionDep, user: CurrentUserDep
) -> HotkeyCredentialPublic:
    """A new shortcut secret, shown once. The previous one stops working."""
    credential, token = await study_crud.replace_credential(session, user.id)
    await meeting_governance_crud.record_audit(
        session,
        action="study.hotkey.create",
        actor=user,
        target={"hint": credential.hint},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await session.refresh(credential)
    return HotkeyCredentialPublic(
        exists=True,
        hint=credential.hint,
        created_at=credential.created_at,
        token=token,
    )


@router.delete("/study/hotkey/credential", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_hotkey_credential(session: SessionDep, user: CurrentUserDep) -> None:
    if await study_crud.revoke_credential(session, user.id):
        await meeting_governance_crud.record_audit(
            session,
            action="study.hotkey.revoke",
            actor=user,
            request_id=request_id_ctx.get(),
        )
    await session.commit()


# --- the press ----------------------------------------------------------------


def _text(message: str, code: int = 200, outcome: str = "error") -> PlainTextResponse:
    return PlainTextResponse(
        message + "\n", status_code=code, headers={"X-Study-Outcome": outcome}
    )


async def _hotkey_user(
    session: SessionDep, authorization: str | None
) -> User | PlainTextResponse:
    """The user a shortcut secret belongs to. The secret opens nothing else:
    every other route requires a signed-in session."""
    scheme, _, token = (authorization or "").partition(" ")
    user = None
    if scheme.lower() == "bearer" and token.strip():
        user = await study_crud.user_for_token(session, token.strip())
    if user is None:
        return _text(
            "This shortcut is not connected to Root Transcribe. Create a new "
            "shortcut key under Set up Mac shortcut.",
            status.HTTP_401_UNAUTHORIZED,
        )
    await session.commit()  # last_used_at
    limit = await get_rate_limiter().hit(
        f"rl:hotkey:{user.id}", per_minute=HOTKEY_PER_MINUTE
    )
    if not limit.allowed:
        return _text(
            "Too many presses. Wait a moment.", status.HTTP_429_TOO_MANY_REQUESTS
        )
    return user


_ERRORS: dict[str, tuple[int, str]] = {
    "paused": (409, "The recording is paused. Resume it to take notes."),
    "sharing_lost": (
        409,
        "The shared tab is no longer available. Share it again in Root Transcribe.",
    ),
    "stale": (408, "The press arrived too late to capture that moment. Press again."),
    "storage": (500, "The note could not be saved in the browser."),
    "not_recording": (409, "No recording with study notes is running."),
    "busy": (409, "The recorder is busy. Press again."),
}


def _answer(result: dict) -> PlainTextResponse:
    """The sentence the Mac shows for what the browser did."""
    outcome = result.get("outcome")
    clock = study_export.clock
    if outcome == "started":
        return _text(
            f"Note started · {clock(result.get('captured_ms') or 0)}", 200, "started"
        )
    if outcome == "saved":
        start, end = result.get("audio_start_ms"), result.get("audio_end_ms")
        if not result.get("has_audio", True) or start is None or end is None:
            return _text("Note saved · screenshot only (no tab audio)", 200, "saved")
        return _text(f"Note saved · {clock(start)}–{clock(end)}", 200, "saved")
    code, message = _ERRORS.get(
        result.get("reason") or "", (500, "The note could not be taken.")
    )
    return _text(message, code)


@router.post("/study/hotkey", response_class=PlainTextResponse)
async def press_hotkey(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_invocation_id: Annotated[str | None, Header(max_length=80)] = None,
) -> PlainTextResponse:
    """One press of the shortcut: start a note, or stop the open one.

    The answer is a plain sentence. A repeated request with the same
    ``X-Invocation-Id`` returns the first answer and never acts twice.
    """
    user = await _hotkey_user(session, authorization)
    if isinstance(user, PlainTextResponse):
        return user
    user_id = user.id
    invocation = (x_invocation_id or "").strip() or uuid.uuid4().hex
    bus = study_commands.get_command_bus()
    stored = await bus.result(user_id, invocation)
    if stored is not None:
        return _answer(stored)
    recorders = await bus.recorders(user_id)
    if not recorders:
        return _text(
            "No recording with study notes is running in Root Transcribe. "
            "Start one, or open its browser tab if it lost the connection.",
            status.HTTP_409_CONFLICT,
        )
    if len(recorders) > 1:
        return _text(
            "Two recorders are open for your account. Stop one of them, so "
            "the press has one place to go.",
            status.HTTP_409_CONFLICT,
        )
    command = {
        "invocation": invocation,
        "action": "toggle",
        "sent_ms": int(time.time() * 1000),
        "stale_after_ms": STALE_COMMAND_MS,
    }
    try:
        result = await bus.deliver(
            user_id, command, timeout=study_commands.ACK_TIMEOUT_SECONDS
        )
    except study_commands.NotConnected:
        return _text(
            "The recorder is not connected. Open its browser tab; it "
            "reconnects by itself.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if result is None:
        return _text(
            "The press reached the recorder, but it did not confirm that the "
            "note was saved. Check the notes in Root Transcribe.",
            status.HTTP_504_GATEWAY_TIMEOUT,
        )
    return _answer(result)


@router.post("/study/hotkey/test", response_class=PlainTextResponse)
async def test_hotkey(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> PlainTextResponse:
    """Check the shortcut without taking a note."""
    user = await _hotkey_user(session, authorization)
    if isinstance(user, PlainTextResponse):
        return user
    name = user.full_name or user.email
    recorders = await study_commands.get_command_bus().recorders(user.id)
    if len(recorders) == 1:
        state = "ready"
    elif recorders:
        state = "open in two places"
    else:
        state = "not running"
    return _text(f"Connected as {name}. Recorder: {state}.", 200, "test")


@router.get("/study/commands/stream")
async def command_stream(
    session: SessionDep,
    user: CurrentUserDep,
    session_id: Annotated[uuid.UUID, Query()],
) -> StreamingResponse:
    """Server-sent events for the browser that records: one ``command`` event
    per press. Open only while a study session is recording."""
    user_id = user.id
    owned = await study_crud.owned_session(
        session, session_id=session_id, owner_id=user_id
    )
    if owned is None:
        raise NotFoundError("Study session not found")
    # The stream lives for the whole recording: give the database connection
    # back now instead of holding it for hours.
    await session.commit()
    bus = study_commands.get_command_bus()
    connection_id = uuid.uuid4().hex

    async def events():
        hello = {"server_ms": int(time.time() * 1000), "connection": connection_id}
        yield f"event: hello\ndata: {json.dumps(hello)}\n\n"
        async for command in bus.listen(user_id, connection_id, str(session_id)):
            if command is None:
                yield ": ping\n\n"
            else:
                yield f"event: command\ndata: {json.dumps(command)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/study/commands/{invocation}/ack", status_code=status.HTTP_204_NO_CONTENT)
async def acknowledge_command(
    invocation: str, payload: CommandAck, user: CurrentUserDep
) -> None:
    """The recording browser reports what a press did, after saving it."""
    if len(invocation) > 80:
        raise AppError("Invalid invocation", status_code=422)
    await study_commands.get_command_bus().acknowledge(
        user.id, invocation, payload.model_dump()
    )
