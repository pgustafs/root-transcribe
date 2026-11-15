"""Study notes: notebooks of screenshots with transcribed excerpts.

A learner records a browser tab and presses "Take note" in the app. Each press
stores the visible frame and a short piece of the tab's audio, which is
transcribed on its own.

``/workspaces/{id}/study/…`` — notebooks, sessions and notes. Private to their
owner; needs the ``study_notes`` capability.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

import anyio
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    Response,
    UploadFile,
    status,
)

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
from app.crud import audio_object as audio_object_crud
from app.crud import meeting_governance as meeting_governance_crud
from app.crud import meeting_recording as meeting_recording_crud
from app.crud import study as study_crud
from app.crud import task_outbox as task_outbox_crud
from app.crud import transcript_revision as transcript_revision_crud
from app.db.session import SessionDep
from app.models.study import NOTE_TAGS, StudyNote, StudyNotebook, StudySession
from app.models.user import User
from app.schemas.study import (
    NotebookCreate,
    NotebookDetail,
    NotebookPublic,
    NotebookUpdate,
    NoteContext,
    NotePublic,
    NoteUpdate,
    SessionCreate,
    SessionPublic,
    SessionUpdate,
    StudyFeatures,
)
from app.services import study_export, study_source
from app.services.audio_store import get_audio_store, key_namespace
from app.services.task_outbox import try_dispatch

router = APIRouter(tags=["study"])

_WS = "/workspaces/{workspace_id}/study"
_ENABLED = [Depends(ensure_meetings_enabled)]

MAX_IMAGE_BYTES = 25 * 1024 * 1024
#: 16 kHz mono 16-bit is about 1.9 MB a minute; the STT endpoint takes 25 MB.
MAX_AUDIO_BYTES = 24 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

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


@router.get(f"{_WS}/features", dependencies=_ENABLED)
async def features(
    workspace_id: uuid.UUID, session: SessionDep, user: CurrentUserDep
) -> StudyFeatures:
    """What this installation can do with notes."""
    await _guard(session, user, workspace_id)
    has_model = bool(settings.LLM_BASE_URL.strip())
    return StudyFeatures(
        enrich=has_model, web_research=has_model and settings.WEB_SEARCH_ENABLED
    )


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
                text=note.clean_text or note.text,
                comment=note.comment,
                title=note.title,
                tags=tuple(note.tags or ()),
                slide_text=note.slide_text,
                terms=tuple(
                    (t.get("term", ""), t.get("definition", ""))
                    for t in (note.terms or [])
                ),
                explanation=note.explanation,
                question=note.question,
                answer=note.answer or note.answer_short,
                answer_links=tuple(
                    (s.get("title", ""), s.get("url", ""))
                    for s in (note.answer_sources or [])
                    if s.get("kind") == "web" and s.get("url")
                ),
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
        enrich=payload.enrich,
        research_web=payload.research_web,
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


def _speaker_label(label: str) -> str:
    """SPEAKER_00 -> "Speaker 1", as the transcript view shows it."""
    match = re.fullmatch(r"SPEAKER_(\d+)", label or "", flags=re.IGNORECASE)
    return f"Speaker {int(match.group(1)) + 1}" if match else (label or "")


def _headers(user: User) -> dict:
    return {"request_id": request_id_ctx.get(), "user": user.email}


def _tags(value: str | list[str] | None) -> list[str]:
    """The known marks in a fixed order; anything else is ignored."""
    given = value.split(",") if isinstance(value, str) else (value or [])
    wanted = {item.strip() for item in given}
    return [tag for tag in NOTE_TAGS if tag in wanted]


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
    image: Annotated[UploadFile | None, File()] = None,
    audio: Annotated[UploadFile | None, File()] = None,
    audio_start_ms: Annotated[int | None, Form(ge=0)] = None,
    audio_end_ms: Annotated[int | None, Form(ge=0)] = None,
    end_reason: Annotated[str | None, Form(max_length=30)] = None,
    tags: Annotated[str | None, Form(max_length=100)] = None,
    comment: Annotated[str | None, Form(max_length=10000)] = None,
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

    # A note from a microphone recording has no picture; it needs sound then.
    image_bytes: bytes | None = None
    if image is not None:
        image_bytes = await _read_limited(image, MAX_IMAGE_BYTES, "image")
        if not image_bytes.startswith(_PNG_MAGIC):
            raise AppError("The image must be a PNG file", status_code=422)
    elif audio is None:
        raise AppError("A note needs an image or audio", status_code=422)
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
    image_key = f"{base}.png" if image_bytes is not None else None
    audio_key = f"{base}.wav" if audio_bytes is not None else None
    notebook_id, study_session_id = notebook.id, study_session.id
    try:
        if image_bytes is not None:
            await anyio.to_thread.run_sync(
                store.put, image_key, image_bytes, "image/png"
            )
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

    width, height = _png_size(image_bytes) if image_bytes else (None, None)
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
        tags=_tags(tags),
        comment=(comment or "").strip() or None,
    )
    session.add(note)
    outbox = None
    if audio_bytes is not None:
        # The model work follows the transcription (see the worker task).
        outbox = await _queue_transcription(session, note, user)
    elif study_crud.enrich_wanted(study_session):
        # No speech to wait for: the image can be read at once.
        outbox = await study_crud.queue_enrich(session, note, headers=_headers(user))
    if audio_bytes is None and "question" in note.tags:
        # A question nobody could hear: say so instead of waiting forever.
        note.answer_status, note.answer_error = "failed", "no_speech"
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
    """Change the owner's own words on a note, or attach the player's "copy
    URL at current time" link (it must point at the notebook's own source)."""
    await _guard(session, user, workspace_id)
    note, notebook = await _note(session, user, workspace_id, note_id)
    if payload.comment is not None:
        note.comment = payload.comment.strip() or None
    outbox = None
    answer_outbox = None
    if payload.tags is not None:
        before = set(note.tags or [])
        note.tags = _tags(list(payload.tags))
        # Newly marked "did not understand": ask for the plain explanation.
        if (
            "unclear" in note.tags
            and "unclear" not in before
            and not note.explanation
            and note.enrich_status in ("none", "ready", "failed")
            and note.status not in ("pending", "processing")
            and study_crud.enrich_wanted(
                await study_crud.get_session(session, note.session_id)
            )
        ):
            outbox = await study_crud.queue_enrich(
                session, note, headers=_headers(user)
            )
        # Newly marked as a question: look for an answer.
        if (
            "question" in note.tags
            and "question" not in before
            and note.answer_status in ("none", "failed")
            and note.status == "ready"
            and study_crud.answer_wanted(note)
        ):
            answer_outbox = await study_crud.queue_answer(
                session, note, headers=_headers(user)
            )
    url = payload.source_time_url
    if url is None:
        pass
    elif not url:
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
    for row in (outbox, answer_outbox):
        if row is not None:
            await try_dispatch(session, row)
    await session.refresh(note)
    return _note_public(note)


@router.get(f"{_WS}/notes/{{note_id}}", dependencies=_ENABLED)
async def get_note(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> NotePublic:
    """One note (the recorder polls this for the answer to a question)."""
    await _guard(session, user, workspace_id)
    note, _owner_notebook = await _note(session, user, workspace_id, note_id)
    return _note_public(note)


@router.post(f"{_WS}/notes/{{note_id}}/answer", dependencies=_ENABLED)
async def answer_note(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> NotePublic:
    """Look for an answer to a note's question (again)."""
    await _guard(session, user, workspace_id)
    note, _owner_notebook = await _note(session, user, workspace_id, note_id)
    if not settings.LLM_BASE_URL.strip():
        raise ConflictError("No language model is configured for this installation")
    if note.answer_status in ("pending", "processing") or note.status in (
        "pending",
        "processing",
    ):
        return _note_public(note)
    if "question" not in (note.tags or []):
        note.tags = _tags([*(note.tags or []), "question"])
    if not (note.text or "").strip():
        note.answer_status, note.answer_error = "failed", "no_speech"
        await session.commit()
        await session.refresh(note)
        return _note_public(note)
    outbox = await study_crud.queue_answer(session, note, headers=_headers(user))
    await session.commit()
    if outbox is not None:
        await try_dispatch(session, outbox)
    await session.refresh(note)
    return _note_public(note)


@router.post(f"{_WS}/notes/{{note_id}}/enrich", dependencies=_ENABLED)
async def enrich_note(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> NotePublic:
    """Have the language model work on a note (again): title, tidy text, key
    terms, text on the slide."""
    await _guard(session, user, workspace_id)
    note, _owner_notebook = await _note(session, user, workspace_id, note_id)
    if not settings.LLM_BASE_URL.strip():
        raise ConflictError("No language model is configured for this installation")
    if note.enrich_status in ("pending", "processing") or note.status in (
        "pending",
        "processing",
    ):
        return _note_public(note)
    outbox = await study_crud.queue_enrich(session, note, headers=_headers(user))
    await session.commit()
    if outbox is not None:
        await try_dispatch(session, outbox)
    await session.refresh(note)
    return _note_public(note)


@router.get(f"{_WS}/notes/{{note_id}}/context", dependencies=_ENABLED)
async def note_context(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    seconds: Annotated[int, Query(ge=0, le=600)] = 30,
) -> NoteContext:
    """What was said up to ``seconds`` before and after a note, taken from the
    transcript of the full recording the note was made in."""
    await _guard(session, user, workspace_id)
    note, _owner_notebook = await _note(session, user, workspace_id, note_id)
    study_session = await study_crud.get_session(session, note.session_id)
    recording_id = study_session.recording_id if study_session else None
    if recording_id is None:
        return NoteContext(available=False, reason="not_linked")
    rec = await meeting_recording_crud.get_for_workspace(
        session, workspace_id=workspace_id, recording_id=recording_id
    )
    if rec is None:
        return NoteContext(available=False, reason="not_linked")
    # The same rule as reading the recording's transcript itself.
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    segments = await meeting_recording_crud.list_segments(
        session,
        recording_id=rec.id,
        revision_id=transcript_revision_crud.display_revision_id(rec),
    )
    if not segments:
        return NoteContext(available=False, reason="not_transcribed")
    first = note.audio_start_ms if note.audio_start_ms is not None else note.captured_ms
    last = note.audio_end_ms if note.audio_end_ms is not None else note.captured_ms
    start_ms = max(0, first - seconds * 1000)
    end_ms = last + seconds * 1000
    inside = [s for s in segments if s.end_ms > start_ms and s.start_ms < end_ms]
    names = await meeting_recording_crud.names_for_profiles(
        session, [s.speaker_profile_id for s in inside if s.speaker_profile_id]
    )
    return NoteContext(
        available=True,
        start_ms=start_ms,
        end_ms=end_ms,
        at_start=start_ms <= segments[0].start_ms,
        at_end=end_ms >= segments[-1].end_ms,
        segments=[
            {
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "speaker": meeting_recording_crud.shown_speaker(s, names)
                or _speaker_label(s.speaker_label),
                "text": s.text,
            }
            for s in inside
        ],
    )


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
