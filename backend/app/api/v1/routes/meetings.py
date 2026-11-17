"""Meeting-recording routes — record, transcribe, review and export.

Mounted under the workspace prefix; every handler is gated by the same four-tier
workspace policy as the rest of the workspace API (:func:`app.api.v1.routes.
workspaces._load_authorized`): ``read`` to list/view/play, ``content`` to record,
edit segments, retry, export, or delete. The heavy STT + diarization work
runs in the ``transcription``-queue Celery worker; these endpoints only enqueue it
and expose the poll-able recording row. See ARCHITECTURE.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import anyio
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy import select

from app.api.deps import CurrentUserDep
from app.api.v1.routes.workspaces import (
    _load_authorized,
)
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.logging import request_id_ctx
from app.core.meeting_permissions import (
    MeetingCapability,
    apply_grants,
    base_capabilities,
)
from app.core.metrics import (
    MEETING_CLIENT_EVENTS,
    MEETING_PLAYBACK_FIRST_BYTE_SECONDS,
    MEETING_PLAYBACK_REQUESTS,
    MEETING_UPLOAD_PART_SECONDS,
    MEETING_UPLOADS,
)
from app.crud import audio_object as audio_object_crud
from app.crud import meeting_clip as meeting_clip_crud
from app.crud import meeting_comment as meeting_comment_crud
from app.crud import meeting_governance as meeting_governance_crud
from app.crud import meeting_recording as meeting_recording_crud
from app.crud import meeting_upload as meeting_upload_crud
from app.crud import notification as notification_crud
from app.crud import speaker_consent as speaker_consent_crud
from app.crud import task_outbox as task_outbox_crud
from app.crud import transcript_revision as transcript_revision_crud
from app.crud import transcription_attempt as transcription_attempt_crud
from app.crud import workspace as workspace_crud
from app.db.session import SessionDep
from app.models.meeting_recording import MeetingRecording
from app.models.workspace import Workspace
from app.schemas.meeting import (
    MAX_SPEAKERS,
    MIN_SPEAKERS,
    AudioLayout,
    DlpFindingPublic,
    DlpFindingStatusSet,
    DlpManualFinding,
    DlpTermCreate,
    DlpTermPublic,
    MeetingClientDiagnostics,
    MeetingClientEvent,
    MeetingClipCreate,
    MeetingClipPublic,
    MeetingCommentCreate,
    MeetingCommentPublic,
    MeetingCommentUpdate,
    MeetingGrantPublic,
    MeetingGrantSet,
    MeetingHoldSet,
    MeetingPermissionsPublic,
    MeetingRecordingDetail,
    MeetingRecordingPublic,
    MeetingRetry,
    MeetingRevisionApprove,
    MeetingRevisionPublic,
    MeetingSegmentPublic,
    MeetingSegmentUpdate,
    MeetingSensitivitySet,
    MeetingSpeakerAssign,
    MeetingUploadComplete,
    MeetingUploadSessionCreate,
    MeetingUploadSessionPublic,
    RecordingLanguage,
    RetentionPolicyPublic,
    RetentionPolicySet,
    SpeakerSuggestionPublic,
)
from app.services import media_proxy
from app.services.audio_store import (
    MultipartDisposition,
    discard_multipart,
    get_audio_store,
    head_or_none,
    new_audio_key,
)
from app.services.dependency_health import ensure_transcription_path
from app.services.task_outbox import try_dispatch
from app.services.workspace_access import effective_role
from app.services.workspace_activity import record_activity

logger = logging.getLogger("app.api.meetings")

router = APIRouter(prefix="/workspaces", tags=["meetings"])


def ensure_meetings_enabled() -> None:
    """Gate every meeting endpoint on the deployment flag + a configured store."""
    if not settings.MEETINGS_ENABLED:
        raise AppError(
            "Meeting recording is not enabled on this deployment",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not settings.AUDIO_S3_ENDPOINT_URL:
        raise AppError(
            "Meeting audio storage is not configured",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


# Formats the pipeline's ffmpeg decode step handles; both live browser recordings
# (webm/ogg/mp4) and common uploaded audio files.
_AUDIO_EXTENSIONS = {"webm", "ogg", "opus", "mp3", "mp4", "m4a", "wav", "flac", "aac"}


async def _capabilities_for(
    session: SessionDep, user, workspace, membership
) -> set[MeetingCapability]:
    """Resolve the caller's effective meeting capabilities in a workspace."""
    role = effective_role(user=user, workspace=workspace, membership=membership)
    caps = base_capabilities(user=user, effective_role=role)
    grants = await meeting_governance_crud.list_grants(session, workspace.id)
    return apply_grants(caps, list(grants), user_id=user.id, effective_role=role)


async def _require_cap(
    session: SessionDep,
    user,
    workspace_id: uuid.UUID,
    capability: MeetingCapability,
) -> None:
    """Enforce one audio capability (issue #6, E1) — in the API, never UI-only.

    Runs after ``_load_authorized`` (membership already verified); resolves the
    default role→capability map plus per-workspace grant overrides.
    """
    workspace = await session.get(Workspace, workspace_id)
    membership = await workspace_crud.get_membership(
        session, workspace_id=workspace_id, user_id=user.id
    )
    caps = await _capabilities_for(session, user, workspace, membership)
    if capability not in caps:
        raise PermissionDeniedError(
            f"You do not have the '{capability}' permission in this workspace"
        )


#: How long a completion claim is trusted before another caller may reclaim it
#: (issue #37 r10). Long enough for a slow multipart finalize, short enough that
#: a dead owner does not strand the session — and the client's own validation
#: watcher polls for minutes, so a reclaim well inside that window is invisible.
COMPLETION_LEASE_SECONDS = 120

#: How often an admitted part write refreshes ``inflight_since`` while it runs.
#: The heartbeat is what makes the freshness cutoff SOUND (issue #37 r18): the
#: cutoff is COMPLETION_LEASE_SECONDS, so a live writer — however slow its
#: client streams — never looks leaked, and a counter that stops refreshing
#: really does belong to a dead process. Must be well under the lease; a test
#: pins the invariant.
WRITE_HEARTBEAT_SECONDS = 30.0

#: How long cancellation waits for an ALREADY-ADMITTED part write to finish
#: before giving up. On timeout the claim is released and the client gets a
#: retryable 503 — never an erasure racing a live writer (issue #37 r18).
CANCEL_DRAIN_SECONDS = 10.0
_CANCEL_DRAIN_POLL_SECONDS = 0.2


def _completion_in_progress() -> AppError:
    """Another caller is finishing this completion RIGHT NOW.

    An explicit, retryable outcome (issue #37 review round 11). Answering 202
    with the still-`uploading` recording made the client mark the meeting
    awaiting-validation and never call completion again — so if the holder then
    died, nothing performed the reclaim and the recording was silently stranded.
    425 says "come back": the retry after the lease expires reclaims and
    finishes the job.
    """
    return AppError(
        "This upload is still being completed; retry shortly.",
        status_code=status.HTTP_425_TOO_EARLY,
    )


def _completion_lease_expired(upload, stale_before: datetime) -> bool:
    """Whether a `completing` claim is stale enough to reclaim.

    A row with no timestamp predates the lease (or was written by an older
    version), so it is reclaimable immediately rather than stuck forever.
    """
    claimed_at = getattr(upload, "completing_at", None)
    if claimed_at is None:
        return True
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=UTC)
    return claimed_at < stale_before


def _parse_client_diagnostics(raw: str | None) -> dict | None:
    """Validate the picker's JSON diagnostics form field (issue #37 r8).

    Goes through the SAME closed-token model as the resumable path, so nothing
    caller-chosen can reach the operator log by this route either. Malformed or
    hostile input is refused rather than silently dropped — a diagnostic the
    caller shaped is worse than none.
    """
    if raw is None or raw == "":
        return None
    try:
        parsed = MeetingClientDiagnostics.model_validate_json(raw)
    except ValidationError as exc:
        raise AppError(
            "Invalid client diagnostics",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        ) from exc
    return parsed.model_dump(exclude_none=True)


async def _heartbeat_inflight(session_id: uuid.UUID) -> None:
    """Refresh an admitted write's ``inflight_since`` for as long as it runs.

    Runs on its OWN session (the request session is busy streaming the body).
    Best-effort by design: a missed beat only matters if it is missed for the
    whole lease, and that is exactly the dead-process case the cutoff exists
    to reap.
    """
    from app.db.session import AsyncSessionLocal

    while True:
        await anyio.sleep(WRITE_HEARTBEAT_SECONDS)
        try:
            async with AsyncSessionLocal() as beat:
                await meeting_upload_crud.touch_inflight(beat, session_id=session_id)
        except Exception:  # noqa: BLE001 — the cutoff is the backstop
            logger.warning(
                "inflight heartbeat failed for upload session %s", session_id
            )


async def _authorize_session_writer(
    session: SessionDep,
    user,
    workspace_id: uuid.UUID,
    upload,
) -> None:
    """Only the person who OPENED an upload session may write to it.

    A session is one person's in-flight recording, addressed by a UUID the
    client holds. Workspace content access is NOT enough (issue #37 review
    round 15): another editor who obtains that id could otherwise overwrite its
    parts or complete it on the uploader's behalf — and would keep that power
    after their RECORD grant was revoked, because nothing downstream of session
    creation re-checked the capability at all.

    Sessions with no recorded creator (rows written before attribution existed)
    cannot be matched to anyone, so they fall back to the capability alone
    rather than becoming permanently unwritable.
    """
    if upload.created_by_id is not None and upload.created_by_id != user.id:
        raise PermissionDeniedError("This upload session belongs to another user")
    await _require_cap(session, user, workspace_id, MeetingCapability.RECORD)


async def _require_any_cap(
    session: SessionDep,
    user,
    workspace_id: uuid.UUID,
    capabilities: tuple[MeetingCapability, ...],
) -> None:
    """Enforce that the caller holds AT LEAST ONE of ``capabilities``.

    Some surfaces are reachable from more than one grant — the recorder
    diagnostics endpoint serves both the in-app recorder (RECORD) and the file
    picker (UPLOAD), which are independently grantable (issue #37 r8).
    """
    workspace = await session.get(Workspace, workspace_id)
    membership = await workspace_crud.get_membership(
        session, workspace_id=workspace_id, user_id=user.id
    )
    caps = await _capabilities_for(session, user, workspace, membership)
    if not any(capability in caps for capability in capabilities):
        raise PermissionDeniedError(
            "You do not have permission to record or upload audio in this workspace"
        )


async def _require_sensitive_cap(
    session: SessionDep,
    user,
    workspace_id: uuid.UUID,
    rec: MeetingRecording,
    capability: MeetingCapability,
) -> None:
    """Sensitivity gate (issue #6, E2): on a ``restricted`` recording, the
    high-exfiltration capabilities (download/export/clip) additionally require
    an explicit USER-scoped allow grant — role defaults are not enough."""
    await _require_cap(session, user, workspace_id, capability)
    if rec.sensitivity != "restricted":
        return
    from app.models.user import Role

    if user.role == Role.ADMIN:
        return
    grants = await meeting_governance_crud.list_grants(session, workspace_id)
    explicitly_allowed = any(
        g.user_id == user.id and g.capability == capability.value and g.allow
        for g in grants
    )
    if not explicitly_allowed:
        raise PermissionDeniedError(
            "This recording is restricted; ask an administrator for an "
            f"explicit '{capability}' grant"
        )


def _require_consent_attestation(participant_consent: bool) -> None:
    """Server-side recording policy (issue #6): when the deployment requires a
    participant-consent attestation, refuse recordings without one — the UI
    checkbox alone gated nothing before."""
    if settings.MEETING_REQUIRE_CONSENT and not participant_consent:
        raise AppError(
            "Recording requires attesting that participants were informed "
            "and consent to being recorded",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


def _stamp_consent_evidence(rec, *, method: str, actor_email: str) -> None:
    """Record what was attested, by whom, and under which policy version."""
    if rec.participant_consent:
        rec.consent_policy_version = settings.CONSENT_POLICY_VERSION
        rec.consent_method = method
        rec.consent_actor_email = actor_email
        rec.consent_recorded_at = datetime.now(UTC)


async def _segments_public(session: SessionDep, segments) -> list[MeetingSegmentPublic]:
    """Serialize segments, resolving each speaker's display name."""
    profile_ids = [s.speaker_profile_id for s in segments if s.speaker_profile_id]
    names = await meeting_recording_crud.names_for_profiles(session, profile_ids)
    out: list[MeetingSegmentPublic] = []
    for s in segments:
        item = MeetingSegmentPublic.model_validate(s)
        item.speaker_name = meeting_recording_crud.shown_speaker(s, names)
        out.append(item)
    return out


@router.post(
    "/{workspace_id}/recordings",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(ensure_meetings_enabled),
        Depends(ensure_transcription_path),
    ],
)
async def create_recording(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    participant_consent: Annotated[bool, Form()] = False,
    requested_language: Annotated[RecordingLanguage, Form()] = "auto",
    expected_speakers: Annotated[
        int | None, Form(ge=MIN_SPEAKERS, le=MAX_SPEAKERS)
    ] = None,
    audio_layout: Annotated[AudioLayout, Form()] = "mixed",
    mic_speakers: Annotated[int | None, Form(ge=MIN_SPEAKERS, le=MAX_SPEAKERS)] = None,
    # Same privacy-safe recorder context the resumable path sends, as a JSON
    # form field (issue #37 r8) — otherwise a picker upload that the worker
    # later rejects would carry no client attribution at all.
    client_diagnostics: Annotated[str | None, Form()] = None,
) -> MeetingRecordingPublic:
    """Upload a meeting recording and start transcription (202 + the pending row).

    Accepts both live browser recordings (webm/opus) and uploaded audio files —
    anything ffmpeg can decode from the allowlist below.
    """
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.UPLOAD)
    _require_consent_attestation(participant_consent)
    diagnostics = _parse_client_diagnostics(client_diagnostics)
    filename = file.filename or "recording.webm"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _AUDIO_EXTENSIONS:
        raise AppError(
            "Unsupported audio format; use one of: "
            + ", ".join(sorted(_AUDIO_EXTENSIONS)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    # Stream the upload through a spool (memory-bounded) instead of the pre-#6
    # unbounded read() — a 500 MB recording no longer occupies API memory.
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.AUDIO_MAX_BYTES:
                limit_mb = settings.AUDIO_MAX_BYTES // (1024 * 1024)
                raise AppError(
                    f"Recording exceeds the {limit_mb} MB limit",
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )
            spool.write(chunk)
        if size == 0:
            raise AppError(
                "Empty audio upload", status_code=status.HTTP_400_BAD_REQUEST
            )
        spool.seek(0)
        content_type = file.content_type or "audio/webm"
        key = new_audio_key(workspace_id, filename)
        store = get_audio_store()
        # Intent-first (review finding: external-write-before-DB windows): the
        # audio_object row commits BEFORE the store write, so a crash mid-put
        # can never leave an untracked blob — worst case is a tracked row
        # whose delete later confirms trivially (NoSuchKey = success).
        intent = await audio_object_crud.create(
            session,
            bucket=settings.AUDIO_S3_BUCKET,
            key=key,
            kind="meeting_recording",
            workspace_id=workspace_id,
            size_bytes=size,
            content_type=content_type,
        )
        await session.commit()
        try:
            await anyio.to_thread.run_sync(store.put_file, key, spool, content_type)
        except Exception:
            await audio_object_crud.request_delete(
                session, intent, reason="upload_failed"
            )
            await session.commit()
            raise

    # Recording + attempt + outbox commit in ONE transaction (issue #6): a
    # crash or broker outage after this point can no longer strand a pending
    # recording no worker will ever pick up — the dispatcher/reconciler beats
    # deliver from the durable outbox row.
    recording = await meeting_recording_crud.create(
        session,
        workspace_id=workspace_id,
        title=title.strip() or "Untitled meeting",
        audio_key=key,
        audio_content_type=content_type,
        size_bytes=size,
        participant_consent=participant_consent,
        requested_language=requested_language,
        expected_speakers=expected_speakers,
        audio_layout=audio_layout,
        mic_speakers=mic_speakers,
        actor=user,
        commit=False,
    )
    if diagnostics is not None:
        recording.client_diagnostics = diagnostics
    _stamp_consent_evidence(
        recording, method="upload_attestation", actor_email=user.email
    )
    intent.ref_id = recording.id
    attempt = await transcription_attempt_crud.create(
        session,
        recording_id=recording.id,
        actor=user,
        config={"filename": filename},
    )
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.transcribe_attempt",
        queue="transcription",
        payload={"attempt_id": str(attempt.id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"attempt:{attempt.id}:send:1",
    )
    # record_activity commits — calling it here lands the event in the SAME
    # transaction as the recording/attempt/outbox rows (review finding: a
    # crash between the two commits lost the feed event).
    await record_activity(
        session,
        workspace_id=workspace_id,
        actor=user,
        event_type="meeting.recorded",
        payload={"recording_id": str(recording.id), "title": recording.title},
    )
    MEETING_UPLOADS.labels(result="completed").inc()
    await try_dispatch(session, outbox)
    await session.refresh(recording)
    return MeetingRecordingPublic.model_validate(recording)


@router.get(
    "/{workspace_id}/recordings",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_recordings(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    limit: int = 100,
    offset: int = 0,
    before_created_at: datetime | None = None,
    before_id: uuid.UUID | None = None,
) -> list[MeetingRecordingPublic]:
    """A page of the workspace's recordings, newest first (``limit`` ≤ 200).

    Pass the previous page's last row as ``before_created_at``+``before_id``
    for stable keyset paging — concurrent inserts/deletes shift ``offset``
    pages (duplicated or skipped rows) but never a cursor page.
    """
    await _load_authorized(session, user, workspace_id, level="read")
    if (before_created_at is None) != (before_id is None):
        # A half cursor silently falling back to page one would hand the
        # caller duplicate rows without telling them (review round 5).
        raise AppError(
            "before_created_at and before_id must be provided together",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    rows = await meeting_recording_crud.list_for_workspace(
        session,
        workspace_id=workspace_id,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
        before=(
            (before_created_at, before_id)
            if before_created_at is not None and before_id is not None
            else None
        ),
    )
    return [MeetingRecordingPublic.model_validate(r) for r in rows]


async def _load_recording(
    session: SessionDep, workspace_id: uuid.UUID, recording_id: uuid.UUID
) -> MeetingRecording:
    rec = await meeting_recording_crud.get_for_workspace(
        session, workspace_id=workspace_id, recording_id=recording_id
    )
    if rec is None:
        raise NotFoundError("Recording not found")
    return rec


@router.get(
    "/{workspace_id}/recordings/{recording_id}",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def get_recording(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingRecordingDetail:
    """One recording plus its transcript segments (the poll payload).

    Segments are scoped to the DISPLAY revision — the approved one when it
    exists, else the newest. Transcript content is included only when the caller
    has the view-transcript capability.
    """
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="read"
    )
    caps = await _capabilities_for(session, user, workspace, membership)
    rec = await _load_recording(session, workspace_id, recording_id)
    # Transcript CONTENT is capability-gated (review finding): a member whose
    # view_transcript was deny-granted still polls status but gets no text.
    if MeetingCapability.VIEW_TRANSCRIPT in caps:
        segments = await meeting_recording_crud.list_segments(
            session,
            recording_id=recording_id,
            revision_id=transcript_revision_crud.display_revision_id(rec),
        )
    else:
        segments = []
    # Build from the base public model so the ``segments`` field never triggers a
    # lazy async relationship load on the ORM object.
    base = MeetingRecordingPublic.model_validate(rec)
    held = (
        await meeting_governance_crud.active_hold(
            session,
            scope_pairs=[
                ("recording", recording_id),
                ("workspace", workspace_id),
            ],
        )
        is not None
    )
    return MeetingRecordingDetail(
        **base.model_dump(),
        segments=await _segments_public(session, segments),
        legal_hold=held,
    )


@router.head(
    "/{workspace_id}/recordings/{recording_id}/audio",
    dependencies=[Depends(ensure_meetings_enabled)],
    include_in_schema=False,
)
@router.get(
    "/{workspace_id}/recordings/{recording_id}/audio",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def get_recording_audio(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    token: str | None = None,
) -> Response:
    """TRUE streaming playback: authenticated byte-range proxy (issue #6).

    Implements 206/416 with Content-Range/Content-Length/Accept-Ranges/ETag so
    the browser seeks without downloading the whole object; ``If-Range`` with a
    stale ETag falls back to a full 200 (e.g. a redacted rendition replaced the
    object mid-session). Size/etag come from the audio_object row — no S3 HEAD
    per request. Proxied (not presigned) so the in-pod store never needs to be
    browser-reachable.
    """
    await _authorize_playback(request, session, workspace_id, recording_id, token)
    return await _serve_audio(request, session, workspace_id, recording_id)


_PLAYBACK_TOKEN_TTL_SECONDS = 600


def _make_playback_token(recording_id: uuid.UUID, user_id: uuid.UUID) -> str:
    import time as _time

    import jwt as _jwt

    payload = {
        "sub": str(user_id),
        "rec": str(recording_id),
        "purpose": "playback",
        "exp": int(_time.time()) + _PLAYBACK_TOKEN_TTL_SECONDS,
    }
    return _jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def _authorize_playback(
    request: Request,
    session: SessionDep,
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    token: str | None,
) -> None:
    """Media auth: a short-lived playback token (the <audio> element cannot
    send Authorization headers) OR the normal bearer flow."""
    import jwt as _jwt

    from app.api.deps import get_current_user, oauth2_scheme
    from app.core.exceptions import AuthenticationError
    from app.core.oidc import get_token_verifier

    if token:
        try:
            payload = _jwt.decode(
                token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
        except _jwt.PyJWTError as exc:
            raise AuthenticationError("Invalid playback token") from exc
        if payload.get("purpose") != "playback" or payload.get("rec") != str(
            recording_id
        ):
            raise AuthenticationError("Invalid playback token")
        return
    bearer = await oauth2_scheme(request)
    user = await get_current_user(request, session, bearer, get_token_verifier())
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="read"
    )
    # Same gate as the token mint (review finding): bearer access to the raw
    # audio must clear the LISTEN capability, not just workspace read.
    caps = await _capabilities_for(session, user, workspace, membership)
    if MeetingCapability.LISTEN not in caps:
        from app.core.exceptions import PermissionDeniedError as _Denied

        raise _Denied("You do not have the 'listen' permission in this workspace")


@router.post(
    "/{workspace_id}/recordings/{recording_id}/audio/session",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def create_playback_session(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> dict[str, str]:
    """Mint a short-lived playback URL for the <audio> element (issue #6).

    Authorization happens HERE (workspace membership); the returned token then
    lets the media element stream/seek with Range requests for its TTL.
    """
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.LISTEN)
    rec = await _load_recording(session, workspace_id, recording_id)
    if not rec.audio_key or rec.audio_deleted:
        raise NotFoundError("Audio is no longer available for this recording")
    token = _make_playback_token(recording_id, user.id)
    await meeting_governance_crud.record_audit(
        session,
        action="audio.listen",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    return {
        "url": (
            f"{settings.API_V1_PREFIX}/workspaces/{workspace_id}"
            f"/recordings/{recording_id}/audio?token={token}"
        ),
        "expires_in_seconds": str(_PLAYBACK_TOKEN_TTL_SECONDS),
    }


@router.get(
    "/{workspace_id}/recordings/{recording_id}/audio/download",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def download_recording_audio(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
) -> Response:
    """Explicit download (Content-Disposition) — independently authorized from
    playback so future policy can allow listening but not exporting."""
    await _load_authorized(session, user, workspace_id, level="read")
    rec_for_label = await _load_recording(session, workspace_id, recording_id)
    await _require_sensitive_cap(
        session, user, workspace_id, rec_for_label, MeetingCapability.DOWNLOAD
    )
    await meeting_governance_crud.record_audit(
        session,
        action="audio.download",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    return await _serve_audio(
        request, session, workspace_id, recording_id, as_download=True
    )


async def _serve_audio(
    request: Request,
    session: SessionDep,
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    *,
    as_download: bool = False,
    title: str | None = None,
) -> Response:
    _started = time.monotonic()
    rec = await _load_recording(session, workspace_id, recording_id)
    if not rec.audio_key or rec.audio_deleted:
        raise NotFoundError("Audio is no longer available for this recording")
    obj = await audio_object_crud.get_by_key(session, rec.audio_key)
    size = (obj.size_bytes if obj else None) or rec.size_bytes or 0
    etag = (obj.etag if obj else None) or f"rec-{rec.id}"
    store = get_audio_store()
    if size <= 0:
        # Legacy row without recorded size — one HEAD fills it in.
        meta = await anyio.to_thread.run_sync(store.head, rec.audio_key)
        size = meta["size"]
        etag = meta["etag"] or etag
        if obj is not None:
            obj.size_bytes = size
            obj.etag = meta["etag"] or obj.etag
            await session.commit()

    media_type = rec.audio_content_type or "audio/webm"
    from email.utils import format_datetime as _http_date

    modified = (obj.updated_at if obj else None) or rec.updated_at
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    base_headers = {
        "Accept-Ranges": "bytes",
        "ETag": f'"{etag}"',
        "Last-Modified": _http_date(modified, usegmt=True),
        "Cache-Control": "private, max-age=0",
    }
    if as_download:
        raw_name = title or rec.title or "meeting"
        safe = (
            "".join(ch for ch in raw_name if ch.isalnum() or ch in " -_").strip()
            or "meeting"
        )
        ext = rec.audio_key.rsplit(".", 1)[-1] if "." in rec.audio_key else "webm"
        base_headers["Content-Disposition"] = f'attachment; filename="{safe}.{ext}"'

    return await _range_proxy_response(
        request,
        store=store,
        key=rec.audio_key,
        size=size,
        etag=etag,
        media_type=media_type,
        base_headers=base_headers,
        kind="download" if as_download else "stream",
        started=_started,
    )


async def _range_proxy_response(
    request: Request,
    *,
    store,
    key: str,
    size: int,
    etag: str,
    media_type: str,
    base_headers: dict[str, str],
    kind: str = "stream",
    started: float | None = None,
) -> Response:
    """The 200/206/416 Range-proxy tail (recordings AND clips) — the shared
    machinery lives in :mod:`app.services.media_proxy` (issue #14); this
    wrapper adds the meetings-specific playback metrics."""

    def _observe(status_code: int) -> None:
        MEETING_PLAYBACK_REQUESTS.labels(kind=kind, status=str(status_code)).inc()
        if started is not None:
            MEETING_PLAYBACK_FIRST_BYTE_SECONDS.observe(time.monotonic() - started)

    return await media_proxy.range_proxy_response(
        request,
        store=store,
        key=key,
        size=size,
        etag=etag,
        media_type=media_type,
        base_headers=base_headers,
        observe=_observe,
    )


@router.patch(
    "/{workspace_id}/recordings/{recording_id}/segments/{segment_id}",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def update_segment(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    segment_id: uuid.UUID,
    payload: MeetingSegmentUpdate,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingSegmentPublic:
    """Correct a segment: edit its text and/or (re)assign or clear its speaker.

    ``expected_version`` (when sent) makes the edit optimistic — a concurrent
    change 409s instead of being silently overwritten. Every change lands in
    the immutable edit-event history, and editing the revision the workspace
    source was rendered from marks the source ``stale`` (issue #6).
    """
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.EDIT_TRANSCRIPT)
    await _load_recording(session, workspace_id, recording_id)
    segment = await meeting_recording_crud.get_segment(
        session, recording_id=recording_id, segment_id=segment_id
    )
    if segment is None:
        raise NotFoundError("Segment not found")
    if (
        payload.expected_version is not None
        and segment.version != payload.expected_version
    ):
        raise ConflictError("The segment changed since you loaded it")
    before = {
        "text": segment.text,
        "speaker_profile_id": (
            str(segment.speaker_profile_id) if segment.speaker_profile_id else None
        ),
        "speaker_name": segment.speaker_display_name,
    }
    event_type = "text_edit" if payload.text is not None else "speaker_assign"
    if payload.text is not None:
        segment.text = payload.text
    # A directory person and a typed name replace each other: last one wins.
    if payload.clear_speaker:
        segment.speaker_profile_id = None
        segment.speaker_display_name = None
        segment.speaker_confidence = None
        event_type = "speaker_clear" if payload.text is None else event_type
    elif payload.speaker_name is not None:
        segment.speaker_display_name = payload.speaker_name
        segment.speaker_profile_id = None
        segment.speaker_confidence = None
    elif payload.speaker_profile_id is not None:
        segment.speaker_profile_id = payload.speaker_profile_id
        segment.speaker_display_name = None
        segment.speaker_confidence = None  # human-assigned ⇒ no model confidence
    segment.is_edited = True
    segment.edited_by_id = user.id
    segment.version += 1
    await transcript_revision_crud.record_edit_event(
        session,
        revision_id=segment.revision_id,
        segment_id=segment.id,
        segment_idx=segment.idx,
        event_type=event_type,
        before=before,
        after={
            "text": segment.text,
            "speaker_profile_id": (
                str(segment.speaker_profile_id) if segment.speaker_profile_id else None
            ),
            "speaker_name": segment.speaker_display_name,
        },
        actor=user,
        reason=payload.reason,
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await session.refresh(segment)
    items = await _segments_public(session, [segment])
    return items[0]


@router.post(
    "/{workspace_id}/recordings/{recording_id}/assign-speaker",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def assign_speaker_by_label(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    payload: MeetingSpeakerAssign,
    session: SessionDep,
    user: CurrentUserDep,
) -> dict[str, int]:
    """Assign (or clear) a person for EVERY segment with one diarization label.

    The "I recognize Speaker 1 — that's Bob" bulk correction for meetings whose
    speakers had no enrolled voiceprint. This is a **human annotation only**: no
    voiceprint is derived from the meeting audio (biometric enrollment always
    requires the speaker's own explicit consent — ARCHITECTURE.md).
    """
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.RELABEL_SPEAKERS)
    rec = await _load_recording(session, workspace_id, recording_id)
    revision_id = transcript_revision_crud.display_revision_id(rec)
    if revision_id is None:
        raise ConflictError("This recording has no transcript yet")
    # Clear, a typed name, or a directory person: each replaces the others.
    name = None if payload.clear_speaker else payload.speaker_name
    profile_id = (
        None
        if payload.clear_speaker or name is not None
        else payload.speaker_profile_id
    )
    if profile_id is not None:
        # The target must be a real directory entry (FK would fail late otherwise).
        from app.crud import speaker_profile as speaker_profile_crud

        if await speaker_profile_crud.get(session, profile_id) is None:
            raise NotFoundError("Speaker not found")
    updated = await meeting_recording_crud.assign_speaker_by_label(
        session,
        recording_id=recording_id,
        revision_id=revision_id,
        speaker_label=payload.speaker_label,
        speaker_profile_id=profile_id,
        speaker_display_name=name,
        edited_by_id=user.id,
    )
    if updated:
        await transcript_revision_crud.record_edit_event(
            session,
            revision_id=revision_id,
            segment_id=None,
            segment_idx=None,
            event_type="bulk_assign",
            before={"speaker_label": payload.speaker_label},
            after={
                "speaker_profile_id": str(profile_id) if profile_id else None,
                "speaker_name": name,
                "updated_segments": updated,
            },
            actor=user,
            request_id=request_id_ctx.get(),
        )
        await session.commit()
    return {"updated_segments": updated}


@router.post(
    "/{workspace_id}/recordings/{recording_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(ensure_meetings_enabled),
        Depends(ensure_transcription_path),
    ],
)
async def retry_recording(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    payload: MeetingRetry | None = None,
) -> MeetingRecordingPublic:
    """Re-run transcription for a terminal recording (audio still present).

    ``expected_speakers`` in the body changes how many people speech
    detection looks for (null: detect); left out, the stored choice is kept.
    ``mic_speakers`` does the same for the microphone side of an
    online-meeting recording. What the channels hold cannot be changed: it
    describes the file.

    Safe for completed recordings since transcript revisions (issue #6): the
    rerun persists a NEW machine_candidate revision, carries human edits
    forward, and never touches an approved revision. The new attempt + outbox
    row commit atomically with the status reset.
    """
    from sqlalchemy.exc import IntegrityError

    from app.services.media_validation import is_retryable

    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.RECORD)
    rec = await _load_recording(session, workspace_id, recording_id)
    if rec.status not in ("failed", "completed"):
        raise ConflictError("Recording is still being processed")
    if not rec.audio_key or rec.audio_deleted:
        raise ConflictError("Audio is no longer available to retry")
    # A non-retryable media fault (issue #37: invalid/incomplete/unsupported/
    # oversized) fails identically against the same stored object — refuse the
    # retry with actionable guidance rather than re-queuing work that cannot
    # succeed. A new/recovered recording is the only fix. (Completed recordings
    # have no error_code, so a legitimate re-run is unaffected.)
    if rec.status == "failed" and not is_retryable(rec.error_code):
        raise ConflictError(
            "This recording can't be transcribed and retrying it won't help — "
            "please make a new recording."
        )
    if payload is not None and "expected_speakers" in payload.model_fields_set:
        rec.expected_speakers = payload.expected_speakers
    if (
        payload is not None
        and "mic_speakers" in payload.model_fields_set
        and rec.audio_layout == "mic_tab"
    ):
        rec.mic_speakers = payload.mic_speakers
    rec.status = "pending"
    rec.error = None
    rec.error_code = None
    rec.current_step = None
    # Keep the original container hint — a hardcoded ".webm" breaks decode of
    # uploaded .wav/.m4a recordings on retry.
    ext = rec.audio_key.rsplit(".", 1)[-1] if "." in rec.audio_key else "webm"
    try:
        attempt = await transcription_attempt_crud.create(
            session,
            recording_id=rec.id,
            actor=user,
            # Which speaker count produced this attempt's revision.
            config={
                "filename": f"recording.{ext}",
                "expected_speakers": rec.expected_speakers,
                "audio_layout": rec.audio_layout,
                "mic_speakers": rec.mic_speakers,
            },
        )
        outbox = await task_outbox_crud.enqueue(
            session,
            task_name="tasks.transcribe_attempt",
            queue="transcription",
            payload={"attempt_id": str(attempt.id)},
            headers={"request_id": request_id_ctx.get(), "user": user.email},
            dedupe_key=f"attempt:{attempt.id}:send:1",
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("A transcription attempt is already in flight") from exc
    await try_dispatch(session, outbox)
    await session.refresh(rec)
    return MeetingRecordingPublic.model_validate(rec)


@router.delete(
    "/{workspace_id}/recordings/{recording_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def delete_recording(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    """Delete a recording and its transcript; the audio blob gets a durable
    deletion tombstone driven to confirmation by the worker sweep."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.DELETE)
    rec = await _load_recording(session, workspace_id, recording_id)
    if (
        await meeting_governance_crud.active_hold(
            session,
            scope_pairs=[
                ("recording", recording_id),
                ("workspace", workspace_id),
            ],
        )
        is not None
    ):
        raise ConflictError(
            "legal_hold_active: this recording is on legal hold and cannot be deleted"
        )
    if rec.audio_key and not rec.audio_deleted:
        obj = await audio_object_crud.ensure_for_key(
            session,
            bucket=settings.AUDIO_S3_BUCKET,
            key=rec.audio_key,
            kind="meeting_recording",
            ref_id=rec.id,
            workspace_id=workspace_id,
        )
        await audio_object_crud.request_delete(
            session, obj, reason="user_request", actor=user
        )
    # The row cascade silently removes MeetingClip rows — tombstone their
    # blobs too, or the clip audio would survive untracked (review finding).
    for clip in await meeting_clip_crud.list_for_recording(
        session, recording_id=recording_id
    ):
        if clip.audio_key:
            clip_obj = await audio_object_crud.ensure_for_key(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=clip.audio_key,
                kind="clip",
                ref_id=clip.id,
                workspace_id=workspace_id,
            )
            await audio_object_crud.request_delete(
                session, clip_obj, reason="recording_delete", actor=user
            )
    # Study notes taken in this recording go with it (media and entries).
    from app.crud import study as study_crud

    await study_crud.remove_for_recording(
        session, recording_id=recording_id, workspace_id=workspace_id, actor=user
    )
    await meeting_governance_crud.record_audit(
        session,
        action="recording.delete",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"title": rec.title},
        request_id=request_id_ctx.get(),
    )
    await meeting_recording_crud.delete_recording(session, rec)


# --- Transcript revisions (issue #6) ----------------------------------------


@router.get(
    "/{workspace_id}/recordings/{recording_id}/revisions",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_revisions(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[MeetingRevisionPublic]:
    """A recording's transcript revisions, newest first."""
    await _load_authorized(session, user, workspace_id, level="read")
    # Revision metadata IS transcript content — it carries the free-text
    # approval note and who signed off — so it follows the same capability as
    # the segments themselves (issue #39 review round 1).
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    await _load_recording(session, workspace_id, recording_id)
    rows = await transcript_revision_crud.list_for_recording(session, recording_id)
    return [MeetingRevisionPublic.model_validate(r) for r in rows]


@router.get(
    "/{workspace_id}/recordings/{recording_id}/revisions/{revision_id}/segments",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def get_revision_segments(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    revision_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[MeetingSegmentPublic]:
    """One revision's segments (compare/review reads)."""
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    await _load_recording(session, workspace_id, recording_id)
    revision = await transcript_revision_crud.get_for_recording(
        session, recording_id=recording_id, revision_id=revision_id
    )
    if revision is None:
        raise NotFoundError("Revision not found")
    segments = await meeting_recording_crud.list_segments(
        session, recording_id=recording_id, revision_id=revision_id
    )
    return await _segments_public(session, segments)


@router.post(
    "/{workspace_id}/recordings/{recording_id}/revisions/{revision_id}/approve",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def approve_revision(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    revision_id: uuid.UUID,
    payload: MeetingRevisionApprove,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingRevisionPublic:
    """Make a revision authoritative (supersedes the previously approved one).

    If the workspace source was rendered from another revision it is marked
    ``stale`` — visibly out of sync until re-synced.
    """
    await _load_authorized(session, user, workspace_id, level="content")
    # Deciding a transcript requires being allowed to READ it: the two
    # capabilities are grantable independently, so approve-without-view let
    # someone sign off on — and read the approval notes of — a transcript they
    # are explicitly denied (issue #39 review round 1).
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    await _require_cap(
        session, user, workspace_id, MeetingCapability.APPROVE_TRANSCRIPT
    )
    await _load_recording(session, workspace_id, recording_id)
    # Both decisions serialize on the RECORDING row (issue #39 review round 1).
    # Counting survivors and then discarding in separate statements let two
    # concurrent discards each see two survivors and remove a different one —
    # zero left, which is the very state the floor exists to prevent — and let a
    # discard land on the revision an approve was in the middle of adopting.
    # The revision is read AFTER the lock so it cannot be a pre-lock snapshot.
    locked = await meeting_recording_crud.lock(session, recording_id)
    if locked is None:
        raise NotFoundError("Recording not found")
    rec = locked
    revision = await transcript_revision_crud.get_for_recording(
        session, recording_id=recording_id, revision_id=revision_id
    )
    if revision is None:
        raise NotFoundError("Revision not found")
    if revision.state == "approved":
        return MeetingRevisionPublic.model_validate(revision)  # idempotent
    if revision.state == "discarded":
        raise ConflictError("A discarded revision cannot be approved")
    revision = await transcript_revision_crud.approve(
        session, rec, revision, actor=user, note=payload.note
    )
    approved_public = MeetingRevisionPublic.model_validate(revision)
    approved_number = revision.revision_number

    await meeting_governance_crud.record_audit(
        session,
        action="revision.approve",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"revision_number": approved_number},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await record_activity(
        session,
        workspace_id=workspace_id,
        actor=user,
        event_type="meeting.revision_approved",
        payload={
            "recording_id": str(recording_id),
            "revision_number": approved_number,
        },
    )
    return approved_public


@router.post(
    "/{workspace_id}/recordings/{recording_id}/revisions/{revision_id}/discard",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def discard_revision(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    revision_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingRevisionPublic:
    """Discard a non-approved revision (its segments are kept for the audit
    trail; the display falls back to the newest surviving revision)."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    await _require_cap(
        session, user, workspace_id, MeetingCapability.APPROVE_TRANSCRIPT
    )
    await _load_recording(session, workspace_id, recording_id)
    # Both decisions serialize on the RECORDING row (issue #39 review round 1).
    # Counting survivors and then discarding in separate statements let two
    # concurrent discards each see two survivors and remove a different one —
    # zero left, which is the very state the floor exists to prevent — and let a
    # discard land on the revision an approve was in the middle of adopting.
    # The revision is read AFTER the lock so it cannot be a pre-lock snapshot.
    locked = await meeting_recording_crud.lock(session, recording_id)
    if locked is None:
        raise NotFoundError("Recording not found")
    rec = locked
    revision = await transcript_revision_crud.get_for_recording(
        session, recording_id=recording_id, revision_id=revision_id
    )
    if revision is None:
        raise NotFoundError("Revision not found")
    if revision.state == "approved":
        raise ConflictError("The approved revision cannot be discarded")
    if revision.state == "discarded":
        return MeetingRevisionPublic.model_validate(revision)  # idempotent
    # The floor (issue #39): discarding the LAST surviving revision repoints
    # ``latest_revision_id`` at nothing, leaving a completed recording with no
    # displayable transcript and no un-discard path back.
    if await transcript_revision_crud.count_surviving(session, recording_id) <= 1:
        raise ConflictError(
            "The only transcript revision cannot be discarded — it is the "
            "recording's transcript"
        )
    revision = await transcript_revision_crud.discard(
        session, rec, revision, actor=user
    )
    return MeetingRevisionPublic.model_validate(revision)


# --- Speaker-identification review queue (issue #6) --------------------------


@router.get(
    "/{workspace_id}/recordings/{recording_id}/speaker-suggestions",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_speaker_suggestions(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[SpeakerSuggestionPublic]:
    """Pending uncertain speaker matches for the display revision — the human
    confirmation gate that replaces silent automatic naming."""
    await _load_authorized(session, user, workspace_id, level="read")
    rec = await _load_recording(session, workspace_id, recording_id)
    rows = await speaker_consent_crud.list_pending_for_recording(
        session,
        recording_id=recording_id,
        revision_id=transcript_revision_crud.display_revision_id(rec),
    )
    names = await meeting_recording_crud.names_for_profiles(
        session, [r.suggested_profile_id for r in rows]
    )
    out = []
    for row in rows:
        item = SpeakerSuggestionPublic.model_validate(row)
        item.suggested_profile_name = names.get(row.suggested_profile_id)
        out.append(item)
    return out


@router.post(
    "/{workspace_id}/recordings/{recording_id}/speaker-suggestions/"
    "{suggestion_id}/accept",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def accept_speaker_suggestion(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> dict[str, int]:
    """Confirm a suggestion: assign the person to every segment carrying the
    label on the suggestion's revision (a human annotation — never enrollment)."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.RELABEL_SPEAKERS)
    await _load_recording(session, workspace_id, recording_id)
    suggestion = await speaker_consent_crud.get_suggestion(
        session, recording_id=recording_id, suggestion_id=suggestion_id
    )
    if suggestion is None:
        raise NotFoundError("Suggestion not found")
    if suggestion.status != "pending":
        raise ConflictError("This suggestion was already decided")
    updated = await meeting_recording_crud.assign_speaker_by_label(
        session,
        recording_id=recording_id,
        revision_id=suggestion.revision_id,
        speaker_label=suggestion.speaker_label,
        speaker_profile_id=suggestion.suggested_profile_id,
        edited_by_id=user.id,
    )
    await speaker_consent_crud.decide_suggestion(
        session, suggestion, status="accepted", actor=user
    )
    await meeting_governance_crud.record_audit(
        session,
        action="suggestion.accept",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={
            "speaker_label": suggestion.speaker_label,
            "profile_id": str(suggestion.suggested_profile_id),
        },
        request_id=request_id_ctx.get(),
    )
    await transcript_revision_crud.record_edit_event(
        session,
        revision_id=suggestion.revision_id,
        segment_id=None,
        segment_idx=None,
        event_type="bulk_assign",
        before={"speaker_label": suggestion.speaker_label},
        after={
            "speaker_profile_id": str(suggestion.suggested_profile_id),
            "accepted_suggestion": str(suggestion.id),
            "updated_segments": updated,
        },
        actor=user,
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    return {"updated_segments": updated}


@router.post(
    "/{workspace_id}/recordings/{recording_id}/speaker-suggestions/"
    "{suggestion_id}/reject",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def reject_speaker_suggestion(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> dict[str, str]:
    """Reject a suggestion — the speaker stays anonymous (tracked for FA/FR
    evaluation, never used for automatic enrollment)."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.RELABEL_SPEAKERS)
    await _load_recording(session, workspace_id, recording_id)
    suggestion = await speaker_consent_crud.get_suggestion(
        session, recording_id=recording_id, suggestion_id=suggestion_id
    )
    if suggestion is None:
        raise NotFoundError("Suggestion not found")
    if suggestion.status != "pending":
        raise ConflictError("This suggestion was already decided")
    await speaker_consent_crud.decide_suggestion(
        session, suggestion, status="rejected", actor=user
    )
    await session.commit()
    return {"status": "rejected"}


# --- Resumable multipart uploads (issue #6, wave B1) -------------------------


@router.post(
    "/{workspace_id}/meetings/uploads",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(ensure_meetings_enabled),
        Depends(ensure_transcription_path),
    ],
)
async def create_upload_session(
    workspace_id: uuid.UUID,
    payload: MeetingUploadSessionCreate,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingUploadSessionPublic:
    """Open a resumable multipart upload for a new recording.

    Creates the ``uploading`` recording row and an S3 multipart upload; the
    browser then PUTs bounded, checksummed parts and completes explicitly —
    no full-recording buffer ever exists in browser or API memory.
    """
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.RECORD)
    _require_consent_attestation(payload.participant_consent)
    ext = payload.filename.rsplit(".", 1)[-1].lower() if "." in payload.filename else ""
    if ext not in _AUDIO_EXTENSIONS:
        raise AppError(
            "Unsupported audio format; use one of: "
            + ", ".join(sorted(_AUDIO_EXTENSIONS)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if payload.size_bytes is not None and payload.size_bytes > settings.AUDIO_MAX_BYTES:
        limit_mb = settings.AUDIO_MAX_BYTES // (1024 * 1024)
        raise AppError(
            f"Recording exceeds the {limit_mb} MB limit",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    content_type = payload.content_type or "audio/webm"
    key = new_audio_key(workspace_id, payload.filename)
    store = get_audio_store()
    # Intent-first (review round 4): the session row commits BEFORE the
    # external create_multipart call, so every store-side upload we start is
    # attributable to a row — the reconciliation sweep can then safely skip
    # uploads it cannot attribute (another deployment sharing the bucket)
    # without stranding our own crash orphans.
    recording = await meeting_recording_crud.create(
        session,
        workspace_id=workspace_id,
        title=payload.title.strip() or "Untitled meeting",
        audio_key=key,
        audio_content_type=content_type,
        size_bytes=payload.size_bytes or 0,
        participant_consent=payload.participant_consent,
        requested_language=payload.requested_language,
        expected_speakers=payload.expected_speakers,
        audio_layout=payload.audio_layout,
        mic_speakers=payload.mic_speakers,
        actor=user,
        commit=False,
    )
    recording.status = "uploading"
    _stamp_consent_evidence(
        recording, method="attested_checkbox", actor_email=user.email
    )
    upload = await meeting_upload_crud.create_session(
        session,
        recording_id=recording.id,
        workspace_id=workspace_id,
        key=key,
        s3_upload_id=None,
        filename=payload.filename,
        content_type=content_type,
        size_bytes=payload.size_bytes,
        actor=user,
    )
    await session.commit()
    try:
        s3_upload_id = await anyio.to_thread.run_sync(
            store.create_multipart, key, content_type
        )
    except Exception:
        # Compensate: nothing exists store-side, so the intent rows must not
        # linger as an open session (the recording CASCADE takes the session).
        await session.delete(recording)
        await session.commit()
        raise
    upload.s3_upload_id = s3_upload_id
    await session.commit()
    await session.refresh(upload)
    return MeetingUploadSessionPublic.model_validate(upload)


async def _load_upload_session(
    session: SessionDep, workspace_id: uuid.UUID, session_id: uuid.UUID
):
    upload = await meeting_upload_crud.get_session(
        session, workspace_id=workspace_id, session_id=session_id
    )
    if upload is None:
        raise NotFoundError("Upload session not found")
    return upload


@router.put(
    "/{workspace_id}/meetings/uploads/{session_id}/parts/{part_number}",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def upload_session_part(
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    part_number: int,
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    x_part_sha256: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    """Upload one bounded, checksummed part (retry-idempotent).

    The body is read in chunks with a hard cap of the session's part size (+
    slack), so API memory per request is constant regardless of meeting length.
    A checksum mismatch is a 422 the client answers by re-sending the part.
    """
    _started = time.monotonic()
    await _load_authorized(session, user, workspace_id, level="content")
    upload = await _load_upload_session(session, workspace_id, session_id)
    await _authorize_session_writer(session, user, workspace_id, upload)
    # ADMIT the write and check the session is live in ONE conditional UPDATE.
    # Checking first and uploading afterwards is a read-then-write: the audio
    # deletion fence can land in between, and this part would then complete
    # after every abort/verify round — AWS explicitly permits an already-running
    # UploadPart to succeed after an abort — leaving audio the store has just
    # reported as erased (issue #37 review round 17b). The counter this takes is
    # what the fence waits to drain.
    if not await meeting_upload_crud.begin_part_write(session, session_id=upload.id):
        # A part sent after completion started IS a conflict — completion
        # idempotency belongs on the completion endpoint, which can actually
        # return a recording. This one's contract is dict[str, int] (issue #37
        # review round 9: a recording returned here would fail response
        # validation and surface as a 500).
        raise ConflictError("This upload session is no longer active")
    heartbeat = asyncio.create_task(_heartbeat_inflight(upload.id))
    try:
        return await _write_part(
            session=session,
            upload=upload,
            request=request,
            part_number=part_number,
            x_part_sha256=x_part_sha256,
            started=_started,
        )
    finally:
        heartbeat.cancel()
        # Release the admission even when the write failed — a leaked counter
        # would stall every future erasure of this key until it ages out.
        try:
            await meeting_upload_crud.end_part_write(session, session_id=upload.id)
        except Exception:  # noqa: BLE001 — the transaction may already be broken
            await session.rollback()
            await meeting_upload_crud.end_part_write(session, session_id=upload.id)


async def _write_part(
    *,
    session: SessionDep,
    upload,
    request: Request,
    part_number: int,
    x_part_sha256: str | None,
    started: float,
) -> dict[str, int]:
    """The body of one admitted part write (see ``upload_session_part``)."""
    if part_number < 1 or part_number > 10_000:
        raise AppError(
            "Part number out of range", status_code=status.HTTP_400_BAD_REQUEST
        )
    if not x_part_sha256:
        raise AppError(
            "X-Part-Sha256 header is required",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    cap = upload.part_size_bytes + 1024 * 1024  # server-chosen size + slack
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > cap:
            raise AppError(
                "Part exceeds the negotiated part size",
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
    if not received:
        raise AppError("Empty part", status_code=status.HTTP_400_BAD_REQUEST)
    digest = hashlib.sha256(bytes(received)).hexdigest()
    if digest != x_part_sha256.lower():
        raise AppError(
            "Part checksum mismatch; re-send the part",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if upload.s3_upload_id is None:
        # Intent row whose store-side upload never materialized (crash in the
        # create gap) — the client can only restart, never PUT into the void.
        raise ConflictError("Upload session is not ready; start a new upload")
    store = get_audio_store()
    etag = await anyio.to_thread.run_sync(
        store.upload_part, upload.key, upload.s3_upload_id, part_number, bytes(received)
    )
    await meeting_upload_crud.upsert_part(
        session,
        upload_session=upload,
        part_number=part_number,
        size_bytes=len(received),
        sha256=digest,
        etag=etag,
    )
    await session.commit()
    MEETING_UPLOAD_PART_SECONDS.observe(time.monotonic() - started)
    return {"part_number": part_number, "size_bytes": len(received)}


@router.post(
    "/{workspace_id}/meetings/uploads/{session_id}/complete",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def complete_upload_session(
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: MeetingUploadComplete,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingRecordingPublic:
    """Verify the manifest, complete the S3 upload, and start transcription.

    The session flip, audio-object registration, recording ``pending`` state,
    attempt, and outbox row commit in ONE transaction (the wave-A2 guarantee).
    """
    await _load_authorized(session, user, workspace_id, level="content")
    upload = await _load_upload_session(session, workspace_id, session_id)
    # Ahead of the idempotent early return below, which hands back a recording.
    await _authorize_session_writer(session, user, workspace_id, upload)
    if upload.status == "completed":
        # IDEMPOTENT (issue #37 review round 8): a client whose completion timed
        # out — or that simply retried — must learn the outcome rather than get
        # an ambiguous 409, which is what made it start a SECOND recording.
        return MeetingRecordingPublic.model_validate(
            await _load_recording(session, workspace_id, upload.recording_id)
        )
    stale_before = datetime.now(UTC) - timedelta(seconds=COMPLETION_LEASE_SECONDS)
    if upload.status == "completing" and not _completion_lease_expired(
        upload, stale_before
    ):
        # Genuinely in flight: another request holds a FRESH lease. Say so
        # EXPLICITLY so the client retries completion (and reclaims if that
        # holder dies) rather than treating the session as done — or as dead,
        # which is what made it start a second recording (r9, r11).
        raise _completion_in_progress()
    if upload.status not in ("active", "completing"):
        # Aborted/expired: genuinely dead, and there is nothing to return.
        raise ConflictError("This upload session is no longer active")
    # `completing` with an EXPIRED lease falls through: the previous owner died
    # or its storage call failed, and the finalize below is idempotent, so this
    # request may reclaim and finish the job (issue #37 review round 10).
    stored = {
        p.part_number: p
        for p in await meeting_upload_crud.list_parts(session, session_id=upload.id)
    }
    manifest = {p.part_number: p.sha256 for p in payload.parts}
    if set(manifest) != set(stored):
        raise ConflictError("The manifest does not match the uploaded parts")
    for number, sha in manifest.items():
        if stored[number].sha256 != sha.lower():
            raise ConflictError(f"Checksum mismatch for part {number}")
    total = sum(p.size_bytes for p in stored.values())
    if total > settings.AUDIO_MAX_BYTES:
        raise AppError(
            "Recording exceeds the size limit",
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    if upload.s3_upload_id is None:
        raise ConflictError("Upload session is not ready; start a new upload")
    # LEASE the completion atomically (issue #37 review rounds 9-10). One
    # conditional UPDATE covers the first claim AND reclaiming a lease whose
    # owner died: a plain read-then-write let two concurrent requests both run
    # the external completion, and a latch (no expiry) left a dead completion
    # `completing` forever. The loser converges on the holder's result.
    lease = await meeting_upload_crud.claim_for_completion(
        session, session_id=upload.id, stale_before=stale_before
    )
    if lease is None:
        await session.refresh(upload)
        if upload.status == "completed":
            return MeetingRecordingPublic.model_validate(
                await _load_recording(session, workspace_id, upload.recording_id)
            )
        raise _completion_in_progress()

    store = get_audio_store()
    # IDEMPOTENT finalize (issue #37 review round 10): the store may ALREADY
    # hold the object — a previous owner can have completed the multipart and
    # then failed on head(), leaving an object with no attempt/outbox row that
    # the expiry sweep would eventually delete as an orphan. Probe first, and
    # only run the external completion when the object genuinely isn't there.
    meta = await anyio.to_thread.run_sync(head_or_none, store, upload.key)
    if meta is None:
        await anyio.to_thread.run_sync(
            store.complete_multipart,
            upload.key,
            upload.s3_upload_id,
            [(n, p.etag) for n, p in stored.items()],
        )
        meta = await anyio.to_thread.run_sync(store.head, upload.key)

    # FENCE (issue #37 review round 11): the storage calls above can outlast the
    # lease, so another caller may legitimately have reclaimed and be finishing
    # too. Only the current lease holder writes recording state; the other
    # abandons rather than committing on top of the new owner.
    if not await meeting_upload_crud.finalize_completion(
        session, session_id=upload.id, lease=lease
    ):
        await session.rollback()
        await session.refresh(upload)
        if upload.status == "completed":
            return MeetingRecordingPublic.model_validate(
                await _load_recording(session, workspace_id, upload.recording_id)
            )
        raise _completion_in_progress()

    rec = await _load_recording(session, workspace_id, upload.recording_id)
    rec.status = "pending"
    rec.size_bytes = meta["size"]
    if payload.client_diagnostics is not None:
        # Privacy-safe recorder context (issue #37 r7). Stored as validated,
        # closed tokens so the WORKER can attach it to any media rejection it
        # emits — the client supplies context, never the verdict.
        rec.client_diagnostics = payload.client_diagnostics.model_dump(
            exclude_none=True
        )
    obj = await audio_object_crud.create(
        session,
        bucket=settings.AUDIO_S3_BUCKET,
        key=upload.key,
        kind="meeting_recording",
        ref_id=rec.id,
        workspace_id=workspace_id,
        size_bytes=meta["size"],
        content_type=upload.content_type,
    )
    obj.etag = meta["etag"]
    attempt = await transcription_attempt_crud.create(
        session,
        recording_id=rec.id,
        actor=user,
        config={"filename": upload.filename},
    )
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.transcribe_attempt",
        queue="transcription",
        payload={"attempt_id": str(attempt.id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"attempt:{attempt.id}:send:1",
    )
    await record_activity(
        session,
        workspace_id=workspace_id,
        actor=user,
        event_type="meeting.recorded",
        payload={"recording_id": str(rec.id), "title": rec.title},
    )
    await try_dispatch(session, outbox)
    await session.refresh(rec)
    return MeetingRecordingPublic.model_validate(rec)


@router.post(
    "/{workspace_id}/meetings/client-events",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def report_client_event(
    workspace_id: uuid.UUID,
    payload: MeetingClientEvent,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    """Record a browser-side recorder diagnostic (issue #37).

    The client validates media BEFORE upload, and can observe assembly gaps and
    recovery outcomes the server never sees. Without this, a browser that starts
    emitting invalid containers is invisible to operators until users complain —
    #37 requires those categories to be observable.

    Deliberately minimal and privacy-safe: every field is a closed enum, a
    bounded integer, or a strictly-patterned MIME, and unknown fields are
    rejected — so nothing free-text can reach the logs through it (a browser
    can call this endpoint, so an unvalidated string here would be a
    log-injection and exfiltration channel). The category alone is the metric
    label (low-cardinality); the correlation fields stay in the
    access-controlled log.

    Gated by the same boundary as recording itself (issue #37 review round 5):
    a read-only member cannot forge recorder telemetry.
    """
    await _load_authorized(session, user, workspace_id, level="content")
    # RECORD *or* UPLOAD (issue #37 review round 8): the two are independently
    # grantable, and an upload-only user hits the picker preflight — refusing
    # their diagnostics would silently drop exactly the events we want.
    await _require_any_cap(
        session,
        user,
        workspace_id,
        (MeetingCapability.RECORD, MeetingCapability.UPLOAD),
    )
    MEETING_CLIENT_EVENTS.labels(event=payload.event).inc()
    logger.info(
        "meeting client event",
        extra={
            "operation": "meeting.client_event",
            "workspace_id": str(workspace_id),
            "event": payload.event,
            "browser_family": payload.browser_family,
            "browser_major": payload.browser_major,
            "container": payload.container,
            "codec": payload.codec,
            "chunk_count": payload.chunk_count,
            "missing_count": payload.missing_count,
            "duplicate_count": payload.duplicate_count,
            "max_gap_ms": payload.max_gap_ms,
            "max_media_gap_ms": payload.max_media_gap_ms,
            "max_skew_ms": payload.max_skew_ms,
            "total_bytes": payload.total_bytes,
        },
    )


@router.delete(
    "/{workspace_id}/meetings/uploads/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def abort_upload_session(
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    """TRUE cancel: abort the S3 upload (discarding parts) and drop the row —
    a cancelled upload leaves nothing behind.

    Cancel destroys STORAGE, so it takes the same lease every other writer
    takes (issue #37 review round 12). The defined semantics are:

    * ``completed`` → 409. The recording exists; cancelling is a delete.
    * ``aborted`` → 204. Cancel is idempotent — a client that retried, or
      cancelled twice, must not be told it failed.
    * a FRESH completion lease → 409. Someone is completing this upload right
      now; aborting its multipart upload underneath it would destroy a live
      recording. The client stops retrying, and the session then either
      completes or (if that owner died) becomes cancellable once the lease
      goes stale.
    * ``expiring`` → 409. The sweep already owns this session's storage.
    * ``active`` or a STALE ``completing`` → cancel proceeds, holding the lease
      so no completion can start between the abort and the status write.

    Cancelling a STALE ``completing`` session may find the multipart upload
    already COMPLETE — its owner died between the store accepting the object
    and the transaction that registers it. Aborting there raises NoSuchUpload,
    which the store treats as "nothing to abort"; the object would then survive
    with no ledger row, and deleting the session and recording removes the last
    thing that could ever find it. So cancellation runs the SAME reconciliation
    the expiry sweep does: HEAD the key, and register + tombstone a completed
    object through the confirmed-deletion lifecycle instead of orphaning it.
    An INDETERMINATE probe destroys nothing and is reported as such (issue #37
    review round 14).
    """
    await _load_authorized(session, user, workspace_id, level="content")
    upload = await meeting_upload_crud.get_session(
        session, workspace_id=workspace_id, session_id=session_id
    )
    if upload is None:
        # IDEMPOTENT even when the row is GONE. A completed cancel deletes the
        # uploading recording, and PostgreSQL's FK cascade takes the session row
        # with it — so on real infrastructure a retried DELETE finds nothing.
        # Answering 404 there made a perfectly successful cancellation look like
        # a failure (the SQLite functional lane, which does not enforce the
        # cascade, hid this — issue #37 review round 13). Authorization has
        # already been checked, so this leaks nothing.
        return
    # OWNERSHIP. Cancellation destroys another person's recording, so workspace
    # content access is not enough: any editor could otherwise discard a
    # colleague's upload mid-flight, bypassing the DELETE capability that guards
    # the very same act once the recording exists (issue #37 review round 14).
    # The uploader may always cancel their own upload; anyone else needs DELETE.
    if upload.created_by_id is None or upload.created_by_id != user.id:
        await _require_cap(session, user, workspace_id, MeetingCapability.DELETE)
    if upload.status == "completed":
        raise ConflictError("This upload already completed")
    if upload.status in ("aborted", "expired"):
        return  # already cancelled — idempotent
    # A CANCELLING claim, not a completing one: `abort_multipart` runs with the
    # store's own timeouts and retries and can outlive the lease, and an
    # ordinary `completing` claim would let a completion reclaim the row,
    # finish the upload, and then have this request delete its recording
    # (issue #37 review round 13). Nothing may complete a `cancelling` session.
    lease = await meeting_upload_crud.claim_for_cancel(
        session,
        session_id=upload.id,
        stale_before=datetime.now(UTC) - timedelta(seconds=COMPLETION_LEASE_SECONDS),
    )
    if lease is None:
        # Name the ACTUAL owner: a client told "being completed" when the
        # expiry sweep already owns the storage would sit there retrying a
        # cancel that can never succeed.
        await session.refresh(upload)
        if upload.status == "expiring":
            raise ConflictError("This upload session is no longer active")
        raise ConflictError(
            "This upload is being completed and cannot be cancelled right now"
        )
    prior_status = upload.status
    # DRAIN admitted part writes before touching storage (issue #37 r18). The
    # claim above stops NEW writes (admission requires `active`), but a write
    # admitted a moment earlier is still streaming — and AWS permits that
    # UploadPart to succeed after the abort, leaving parts that a "clean" 204
    # then orphans: the session row goes with the recording, so nothing owns
    # the key any more and reconciliation skips it.
    drain_deadline = time.monotonic() + CANCEL_DRAIN_SECONDS
    while not await meeting_upload_crud.session_writers_drained(
        session,
        session_id=upload.id,
        stale_before=datetime.now(UTC) - timedelta(seconds=COMPLETION_LEASE_SECONDS),
    ):
        if time.monotonic() >= drain_deadline:
            # Hand the claim straight back and say "come back": erasing around
            # a live writer is the one thing this endpoint must never do.
            await meeting_upload_crud.release_claim(
                session, session_id=upload.id, lease=lease, status=prior_status
            )
            raise AppError(
                "An upload for this session is still in flight; "
                "retry the cancellation shortly",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        await anyio.sleep(_CANCEL_DRAIN_POLL_SECONDS)
    if upload.s3_upload_id is not None:  # NULL id ⇒ nothing exists store-side
        store = get_audio_store()
        try:
            disposition = await anyio.to_thread.run_sync(
                discard_multipart, store, upload.key, upload.s3_upload_id
            )
        except Exception:  # noqa: BLE001 — an INDETERMINATE store, not absence
            # Nothing is destroyed on a guess. Hand the claim straight back so
            # the next attempt is not told the session is busy for two minutes,
            # and say plainly that the outcome is unknown.
            await meeting_upload_crud.release_claim(
                session, session_id=upload.id, lease=lease, status=prior_status
            )
            raise AppError(
                "The object store did not answer; cancellation was not applied",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from None
        if disposition is MultipartDisposition.NEEDS_TOMBSTONE:
            # The key may hold a completed object — the upload finished, or it
            # vanished between the probe and the abort. Registering it keeps the
            # key attributable after the session and recording rows go away, and
            # routes it through the confirmed-deletion lifecycle instead of
            # leaving a blob in the bucket with nothing pointing at it.
            obj = await audio_object_crud.ensure_for_key(
                session,
                bucket=settings.AUDIO_S3_BUCKET,
                key=upload.key,
                kind="meeting_recording",
                ref_id=upload.recording_id,
                workspace_id=workspace_id,
            )
            await audio_object_crud.request_delete(
                session, obj, reason="cancelled_upload_completion", actor=user
            )
    # FENCED: if the abort outlived the lease and the sweep reclaimed the row,
    # this cancel abandons rather than deleting a recording it no longer owns.
    # The storage is gone either way — the reclaiming owner reconciles it.
    if not await meeting_upload_crud.finalize_cancel(
        session, session_id=upload.id, lease=lease
    ):
        await session.rollback()
        raise ConflictError("This upload session is no longer active")
    rec = await meeting_recording_crud.get_for_workspace(
        session, workspace_id=workspace_id, recording_id=upload.recording_id
    )
    if rec is not None and rec.status == "uploading":
        await session.delete(rec)
    await session.commit()


@router.get(
    "/{workspace_id}/recordings/{recording_id}/revisions/{revision_id}/export",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def export_revision(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    revision_id: uuid.UUID,
    format: str,
    session: SessionDep,
    user: CurrentUserDep,
) -> Response:
    """Export one revision as md | txt | srt | vtt | docx | pdf (issue #6, C3).

    SRT/VTT carry the exact segment time ranges so exported transcripts stay
    linkable back to the audio.
    """
    from app.services import transcript_export

    await _load_authorized(session, user, workspace_id, level="read")
    rec = await _load_recording(session, workspace_id, recording_id)
    await _require_sensitive_cap(
        session, user, workspace_id, rec, MeetingCapability.EXPORT
    )
    if format not in transcript_export.EXPORT_FORMATS:
        raise AppError(
            "Unsupported export format; use one of: "
            + ", ".join(transcript_export.EXPORT_FORMATS),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    revision = await transcript_revision_crud.get_for_recording(
        session, recording_id=recording_id, revision_id=revision_id
    )
    if revision is None:
        raise NotFoundError("Revision not found")
    segments = await meeting_recording_crud.list_segments(
        session, recording_id=recording_id, revision_id=revision_id
    )
    if not segments:
        raise ConflictError("This revision has no transcript to export")
    profile_ids = [s.speaker_profile_id for s in segments if s.speaker_profile_id]
    names = await meeting_recording_crud.names_for_profiles(session, profile_ids)
    export_segments = [
        transcript_export.ExportSegment(
            start_ms=s.start_ms,
            end_ms=s.end_ms,
            speaker=(meeting_recording_crud.shown_speaker(s, names) or s.speaker_label),
            text=s.text,
        )
        for s in segments
    ]
    body = await anyio.to_thread.run_sync(
        lambda: transcript_export.render(
            format, title=rec.title, segments=export_segments
        )
    )
    await meeting_governance_crud.record_audit(
        session,
        action="transcript.export",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"format": format, "revision_id": str(revision_id)},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    safe_name = (
        "".join(ch for ch in rec.title if ch.isalnum() or ch in " -_").strip()
        or "transcript"
    )
    return Response(
        content=body,
        media_type=transcript_export.CONTENT_TYPES[format],
        headers={
            "Content-Disposition": (f'attachment; filename="{safe_name}.{format}"')
        },
    )


# --- Capability permissions (issue #6, wave E1) ------------------------------


@router.get(
    "/{workspace_id}/meetings/permissions",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def get_meeting_permissions(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingPermissionsPublic:
    """The caller's effective audio capabilities — the UI shows/hides controls
    from THIS (server-computed), never from client-side role guesses."""
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="read"
    )
    caps = await _capabilities_for(session, user, workspace, membership)
    return MeetingPermissionsPublic(capabilities=sorted(c.value for c in caps))


@router.get(
    "/{workspace_id}/meetings/permissions/grants",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_meeting_grants(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[MeetingGrantPublic]:
    """The workspace's capability overrides (manager+)."""
    await _load_authorized(session, user, workspace_id, level="manage")
    rows = await meeting_governance_crud.list_grants(session, workspace_id)
    return [MeetingGrantPublic.model_validate(r) for r in rows]


@router.put(
    "/{workspace_id}/meetings/permissions/grants",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def set_meeting_grant(
    workspace_id: uuid.UUID,
    payload: MeetingGrantSet,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingGrantPublic:
    """Upsert one capability override (manager+; audited)."""
    await _load_authorized(session, user, workspace_id, level="manage")
    if (payload.role is None) == (payload.user_id is None):
        raise AppError(
            "A grant is scoped to exactly one of: a workspace role or a user",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        MeetingCapability(payload.capability)
    except ValueError as exc:
        raise AppError(
            "Unknown capability", status_code=status.HTTP_400_BAD_REQUEST
        ) from exc
    grant = await meeting_governance_crud.set_grant(
        session,
        workspace_id=workspace_id,
        capability=payload.capability,
        allow=payload.allow,
        role=payload.role,
        user_id=payload.user_id,
        actor=user,
    )
    await meeting_governance_crud.record_audit(
        session,
        action="permission.change",
        actor=user,
        workspace_id=workspace_id,
        target={
            "capability": payload.capability,
            "allow": payload.allow,
            "role": payload.role,
            "user_id": str(payload.user_id) if payload.user_id else None,
        },
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await session.refresh(grant)
    return MeetingGrantPublic.model_validate(grant)


@router.delete(
    "/{workspace_id}/meetings/permissions/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def delete_meeting_grant(
    workspace_id: uuid.UUID,
    grant_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    """Remove one capability override (manager+; audited)."""
    await _load_authorized(session, user, workspace_id, level="manage")
    if not await meeting_governance_crud.delete_grant(
        session, workspace_id=workspace_id, grant_id=grant_id
    ):
        raise NotFoundError("Grant not found")
    await meeting_governance_crud.record_audit(
        session,
        action="permission.change",
        actor=user,
        workspace_id=workspace_id,
        target={"deleted_grant_id": str(grant_id)},
        request_id=request_id_ctx.get(),
    )
    await session.commit()


@router.patch(
    "/{workspace_id}/recordings/{recording_id}/sensitivity",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def set_recording_sensitivity(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    payload: MeetingSensitivitySet,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingRecordingPublic:
    """Set the sensitivity label (approver-gated, audited — issue #6, E2)."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(
        session, user, workspace_id, MeetingCapability.APPROVE_TRANSCRIPT
    )
    rec = await _load_recording(session, workspace_id, recording_id)
    previous = rec.sensitivity
    rec.sensitivity = payload.sensitivity
    await meeting_governance_crud.record_audit(
        session,
        action="sensitivity.change",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"from": previous, "to": payload.sensitivity},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await session.refresh(rec)
    return MeetingRecordingPublic.model_validate(rec)


# --- Legal hold + retention policy (issue #6, wave E3) -----------------------


@router.post(
    "/{workspace_id}/recordings/{recording_id}/hold",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def place_recording_hold(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    payload: MeetingHoldSet,
    session: SessionDep,
    user: CurrentUserDep,
) -> dict[str, str]:
    """Place a legal hold — blocks EVERY purge path until released (audited)."""
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.LEGAL_HOLD)
    await _load_recording(session, workspace_id, recording_id)
    existing = await meeting_governance_crud.active_hold(
        session, scope_pairs=[("recording", recording_id)]
    )
    if existing is not None:
        raise ConflictError("This recording is already on legal hold")
    hold = await meeting_governance_crud.place_hold(
        session,
        scope_type="recording",
        scope_id=recording_id,
        reason=payload.reason,
        actor=user,
    )
    await meeting_governance_crud.record_audit(
        session,
        action="hold.place",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"reason": payload.reason},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    return {"id": str(hold.id), "status": "held"}


@router.delete(
    "/{workspace_id}/recordings/{recording_id}/hold",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def release_recording_hold(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    """Release the recording's legal hold (audited)."""
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.LEGAL_HOLD)
    hold = await meeting_governance_crud.active_hold(
        session, scope_pairs=[("recording", recording_id)]
    )
    if hold is None:
        raise NotFoundError("No active hold on this recording")
    await meeting_governance_crud.release_hold(session, hold, actor=user)
    await meeting_governance_crud.record_audit(
        session,
        action="hold.release",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        request_id=request_id_ctx.get(),
    )
    await session.commit()


@router.get(
    "/{workspace_id}/meetings/retention",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_retention(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[RetentionPolicyPublic]:
    """The workspace's retention overrides (manage_retention)."""
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.MANAGE_RETENTION)
    rows = await meeting_governance_crud.list_retention_policies(session, workspace_id)
    return [RetentionPolicyPublic.model_validate(r) for r in rows]


@router.put(
    "/{workspace_id}/meetings/retention",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def set_retention(
    workspace_id: uuid.UUID,
    payload: RetentionPolicySet,
    session: SessionDep,
    user: CurrentUserDep,
) -> RetentionPolicyPublic:
    """Upsert one per-artifact workspace retention override (audited)."""
    from app.services.retention import RETENTION_ARTIFACTS

    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.MANAGE_RETENTION)
    if payload.artifact not in RETENTION_ARTIFACTS:
        raise AppError(
            "Only raw_audio retention overrides are supported",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    row = await meeting_governance_crud.set_retention_policy(
        session,
        workspace_id=workspace_id,
        artifact=payload.artifact,
        retention_days=payload.retention_days,
        actor=user,
    )
    await meeting_governance_crud.record_audit(
        session,
        action="retention.change",
        actor=user,
        workspace_id=workspace_id,
        target={
            "artifact": payload.artifact,
            "retention_days": payload.retention_days,
        },
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await session.refresh(row)
    return RetentionPolicyPublic.model_validate(row)


# --- transcript comments & mentions (issue #6, wave D4) ---------------------


@router.get(
    "/{workspace_id}/recordings/{recording_id}/comments",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_meeting_comments(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
    include_resolved: bool = False,
) -> list[MeetingCommentPublic]:
    """A recording's comment threads (resolved roots hidden by default)."""
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    await _load_recording(session, workspace_id, recording_id)
    comments = await meeting_comment_crud.list_for_recording(
        session, recording_id=recording_id, include_resolved=include_resolved
    )
    return [MeetingCommentPublic.model_validate(c) for c in comments]


@router.post(
    "/{workspace_id}/recordings/{recording_id}/comments",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def add_meeting_comment(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    payload: MeetingCommentCreate,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingCommentPublic:
    """Comment on a transcript (editor+). Mentions must be workspace members;
    each mention lands an in-app notification deep-linking to the moment."""
    workspace, _ = await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.VIEW_TRANSCRIPT)
    rec = await _load_recording(session, workspace_id, recording_id)
    if payload.parent_id is not None:
        parent = await meeting_comment_crud.get(
            session, workspace_id=workspace_id, comment_id=payload.parent_id
        )
        if parent.recording_id != recording_id or parent.parent_id is not None:
            raise ConflictError("Replies attach to a root comment of this recording")

    # Mentions: only people who can actually open the workspace.
    valid_mentions: list[uuid.UUID] = []
    if payload.mentions:
        member_rows = await workspace_crud.list_members(
            session, workspace_id=workspace_id
        )
        allowed = {m.user_id for m, _email in member_rows}
        if workspace.created_by_id is not None:
            allowed.add(workspace.created_by_id)
        unknown = [uid for uid in payload.mentions if uid not in allowed]
        if unknown:
            raise ConflictError("Mentions must be members of this workspace")
        valid_mentions = list(dict.fromkeys(payload.mentions))

    comment = await meeting_comment_crud.create(
        session,
        workspace_id=workspace_id,
        recording_id=recording_id,
        author=user,
        body=payload.body,
        parent_id=payload.parent_id,
        at_ms=payload.at_ms,
        segment_id=payload.segment_id,
        anchor_quote=payload.anchor_quote,
        mentioned_user_ids=valid_mentions,
    )
    link = f"/workspaces/{workspace_id}/meetings?recording={recording_id}"
    if payload.at_ms is not None:
        link += f"&t={payload.at_ms // 1000}"
    for mentioned_id in valid_mentions:
        if mentioned_id == user.id:
            continue  # no self-notifications
        await notification_crud.create(
            session,
            user_id=mentioned_id,
            type="mention",
            title=f"{user.full_name or user.email} mentioned you",
            body=payload.body[:500],
            link=link,
            payload={
                "workspace_id": str(workspace_id),
                "recording_id": str(recording_id),
                "comment_id": str(comment.id),
                "recording_title": rec.title,
            },
        )
    await record_activity(
        session,
        workspace_id=workspace_id,
        actor=user,
        event_type="meeting.comment_created",
        payload={"recording_id": str(recording_id), "title": rec.title},
    )
    return MeetingCommentPublic.model_validate(comment)


@router.patch(
    "/{workspace_id}/recordings/{recording_id}/comments/{comment_id}",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def update_meeting_comment(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    comment_id: uuid.UUID,
    payload: MeetingCommentUpdate,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingCommentPublic:
    """Edit a comment's body — its **author** only."""
    await _load_authorized(session, user, workspace_id, level="read")
    comment = await meeting_comment_crud.get(
        session, workspace_id=workspace_id, comment_id=comment_id
    )
    if comment.recording_id != recording_id:
        raise NotFoundError("Comment not found")
    if comment.author_id != user.id:
        raise PermissionDeniedError("Only the comment's author can edit it")
    comment = await meeting_comment_crud.update_body(
        session, comment, body=payload.body
    )
    return MeetingCommentPublic.model_validate(comment)


@router.delete(
    "/{workspace_id}/recordings/{recording_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def delete_meeting_comment(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    comment_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    """Delete a comment — its author, or a workspace **owner**/admin (any)."""
    workspace, membership = await _load_authorized(
        session, user, workspace_id, level="read"
    )
    comment = await meeting_comment_crud.get(
        session, workspace_id=workspace_id, comment_id=comment_id
    )
    if comment.recording_id != recording_id:
        raise NotFoundError("Comment not found")
    if comment.author_id != user.id:
        role = effective_role(user=user, workspace=workspace, membership=membership)
        if role != "owner":
            raise PermissionDeniedError(
                "Only the author or a workspace owner can delete this comment"
            )
    await meeting_comment_crud.delete(session, comment)


@router.post(
    "/{workspace_id}/recordings/{recording_id}/comments/{comment_id}/resolve",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def resolve_meeting_comment(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    comment_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingCommentPublic:
    """Resolve a root comment thread (editor+)."""
    return await _set_meeting_comment_resolved(
        session, user, workspace_id, recording_id, comment_id, resolved=True
    )


@router.post(
    "/{workspace_id}/recordings/{recording_id}/comments/{comment_id}/reopen",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def reopen_meeting_comment(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    comment_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingCommentPublic:
    """Reopen a resolved comment thread (editor+)."""
    return await _set_meeting_comment_resolved(
        session, user, workspace_id, recording_id, comment_id, resolved=False
    )


async def _set_meeting_comment_resolved(
    session: SessionDep,
    user: CurrentUserDep,
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    comment_id: uuid.UUID,
    *,
    resolved: bool,
) -> MeetingCommentPublic:
    await _load_authorized(session, user, workspace_id, level="content")
    comment = await meeting_comment_crud.get(
        session, workspace_id=workspace_id, comment_id=comment_id
    )
    if comment.recording_id != recording_id:
        raise NotFoundError("Comment not found")
    if comment.parent_id is not None:
        raise ConflictError("Only a thread's root comment can be resolved")
    comment = await meeting_comment_crud.set_resolved(
        session, comment, resolved=resolved, actor=user
    )
    return MeetingCommentPublic.model_validate(comment)


# --- governed audio clips (issue #6, wave D5) -------------------------------


@router.post(
    "/{workspace_id}/recordings/{recording_id}/clips",
    dependencies=[Depends(ensure_meetings_enabled)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_clip(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    payload: MeetingClipCreate,
    session: SessionDep,
    user: CurrentUserDep,
) -> MeetingClipPublic:
    """Cut a governed clip (capability + sensitivity gated, audited).

    The pending clip and its outbox send-intent commit atomically; the worker
    slices with ffmpeg into a new ``audio_object(kind="clip")``.
    """
    await _load_authorized(session, user, workspace_id, level="content")
    rec = await _load_recording(session, workspace_id, recording_id)
    # A clip is an exfiltration surface: restricted recordings need an
    # explicit user grant on top of the role capability (E2 gate).
    await _require_sensitive_cap(
        session, user, workspace_id, rec, MeetingCapability.CREATE_CLIP
    )
    if not rec.audio_key or rec.audio_deleted:
        raise ConflictError("The recording's audio is no longer available")
    if payload.end_ms <= payload.start_ms:
        raise AppError(
            "end_ms must be greater than start_ms",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if (
        rec.duration_seconds
        and payload.start_ms >= int(rec.duration_seconds * 1000) + 1000
    ):
        raise AppError(
            "start_ms is past the end of the recording",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    clip = await meeting_clip_crud.create_pending(
        session,
        workspace_id=workspace_id,
        recording_id=recording_id,
        title=payload.title,
        start_ms=payload.start_ms,
        end_ms=payload.end_ms,
        revision_id=transcript_revision_crud.display_revision_id(rec),
        actor=user,
    )
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.create_meeting_clip",
        queue="transcription",
        payload={"clip_id": str(clip.id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"clip:{clip.id}:send:1",
    )
    await meeting_governance_crud.record_audit(
        session,
        action="clip.create",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={
            "clip_id": str(clip.id),
            "start_ms": payload.start_ms,
            "end_ms": payload.end_ms,
        },
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await try_dispatch(session, outbox)
    await session.refresh(clip)
    return MeetingClipPublic.model_validate(clip)


@router.get(
    "/{workspace_id}/recordings/{recording_id}/clips",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_clips(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[MeetingClipPublic]:
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.LISTEN)
    await _load_recording(session, workspace_id, recording_id)
    clips = await meeting_clip_crud.list_for_recording(
        session, recording_id=recording_id
    )
    return [MeetingClipPublic.model_validate(c) for c in clips]


@router.delete(
    "/{workspace_id}/recordings/{recording_id}/clips/{clip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def delete_clip(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    clip_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    """Delete a clip — its creator or a delete-capable member; hold-aware.

    The audio object gets a durable ``delete_requested`` tombstone (confirmed
    by the deletion sweep — same honest lifecycle as recordings).
    """
    from app.services import retention

    await _load_authorized(session, user, workspace_id, level="content")
    clip = await meeting_clip_crud.get_for_recording(
        session, recording_id=recording_id, clip_id=clip_id
    )
    if clip is None:
        raise NotFoundError("Clip not found")
    if clip.created_by_id != user.id:
        await _require_cap(session, user, workspace_id, MeetingCapability.DELETE)
    else:
        await _require_cap(session, user, workspace_id, MeetingCapability.CREATE_CLIP)
    held = await session.run_sync(
        lambda sync_session: retention.is_held_sync(
            sync_session,
            ("recording", recording_id),
            ("workspace", workspace_id),
        )
    )
    if held:
        raise ConflictError("legal_hold_active: this recording is under legal hold")
    if clip.audio_key:
        obj = await audio_object_crud.get_by_key(session, clip.audio_key)
        if obj is not None:
            await audio_object_crud.request_delete(
                session, obj, reason="clip_deleted", actor=user
            )
    await meeting_governance_crud.record_audit(
        session,
        action="clip.delete",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"clip_id": str(clip_id)},
        request_id=request_id_ctx.get(),
    )
    await session.delete(clip)
    await session.commit()


@router.post(
    "/{workspace_id}/recordings/{recording_id}/clips/{clip_id}/audio/session",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def create_clip_playback_session(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    clip_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> dict[str, str]:
    """Mint a short-lived playback URL for a ready clip (audited listen)."""
    await _load_authorized(session, user, workspace_id, level="read")
    await _require_cap(session, user, workspace_id, MeetingCapability.LISTEN)
    clip = await meeting_clip_crud.get_for_recording(
        session, recording_id=recording_id, clip_id=clip_id
    )
    if clip is None or clip.status != "ready" or not clip.audio_key:
        raise NotFoundError("Clip audio is not available")
    token = _make_playback_token(clip_id, user.id)
    await meeting_governance_crud.record_audit(
        session,
        action="clip.listen",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={"clip_id": str(clip_id)},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    return {
        "url": (
            f"{settings.API_V1_PREFIX}/workspaces/{workspace_id}"
            f"/recordings/{recording_id}/clips/{clip_id}/audio?token={token}"
        ),
        "expires_in_seconds": str(_PLAYBACK_TOKEN_TTL_SECONDS),
    }


@router.head(
    "/{workspace_id}/recordings/{recording_id}/clips/{clip_id}/audio",
    dependencies=[Depends(ensure_meetings_enabled)],
    include_in_schema=False,
)
@router.get(
    "/{workspace_id}/recordings/{recording_id}/clips/{clip_id}/audio",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def get_clip_audio(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    clip_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    token: str | None = None,
) -> Response:
    """Clip playback through the same authenticated Range proxy as the
    recording (token is clip-bound; no presigned URLs)."""
    await _authorize_playback(request, session, workspace_id, clip_id, token)
    _started = time.monotonic()
    clip = await meeting_clip_crud.get_for_recording(
        session, recording_id=recording_id, clip_id=clip_id
    )
    if clip is None or clip.status != "ready" or not clip.audio_key:
        raise NotFoundError("Clip audio is not available")
    obj = await audio_object_crud.get_by_key(session, clip.audio_key)
    size = (obj.size_bytes if obj else None) or clip.size_bytes or 0
    etag = (obj.etag if obj else None) or f"clip-{clip.id}"
    store = get_audio_store()
    if size <= 0:
        meta = await anyio.to_thread.run_sync(store.head, clip.audio_key)
        size = meta["size"]
        etag = meta["etag"] or etag
    return await _range_proxy_response(
        request,
        store=store,
        key=clip.audio_key,
        size=size,
        etag=etag,
        media_type="audio/webm",
        base_headers={
            "Accept-Ranges": "bytes",
            "ETag": f'"{etag}"',
            "Last-Modified": _clip_http_date(obj, clip),
            "Cache-Control": "private, max-age=0",
        },
        kind="clip",
        started=_started,
    )


def _clip_http_date(obj, clip) -> str:
    from email.utils import format_datetime as _http_date

    modified = (obj.updated_at if obj is not None else None) or clip.updated_at
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    return _http_date(modified, usegmt=True)


# --- DLP detection & redaction (issue #6, wave E4) --------------------------


@router.get(
    "/{workspace_id}/meetings/dlp-terms",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_dlp_terms(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[DlpTermPublic]:
    from app.models.meeting_dlp import DlpTerm

    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.REDACT)
    rows = (
        (
            await session.execute(
                select(DlpTerm)
                .where(DlpTerm.workspace_id == workspace_id)
                .order_by(DlpTerm.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [DlpTermPublic.model_validate(t) for t in rows]


@router.post(
    "/{workspace_id}/meetings/dlp-terms",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def add_dlp_term(
    workspace_id: uuid.UUID,
    payload: DlpTermCreate,
    session: SessionDep,
    user: CurrentUserDep,
) -> DlpTermPublic:
    """Add a workspace detection pattern; an invalid regex is refused here
    rather than silently skipped at scan time."""
    import re as _re

    from app.models.meeting_dlp import DlpTerm

    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.REDACT)
    if payload.is_regex:
        try:
            _re.compile(payload.pattern)
        except _re.error as exc:
            raise AppError(
                f"Invalid regular expression: {exc}",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            ) from exc
    term = DlpTerm(
        workspace_id=workspace_id,
        pattern=payload.pattern,
        is_regex=payload.is_regex,
        label=payload.label,
        created_by_email=user.email,
    )
    session.add(term)
    await session.commit()
    await session.refresh(term)
    return DlpTermPublic.model_validate(term)


@router.delete(
    "/{workspace_id}/meetings/dlp-terms/{term_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def delete_dlp_term(
    workspace_id: uuid.UUID,
    term_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> None:
    from app.models.meeting_dlp import DlpTerm

    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.REDACT)
    term = await session.get(DlpTerm, term_id)
    if term is None or term.workspace_id != workspace_id:
        raise NotFoundError("DLP term not found")
    await session.delete(term)
    await session.commit()


@router.get(
    "/{workspace_id}/recordings/{recording_id}/dlp",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def list_dlp_findings(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> list[DlpFindingPublic]:
    """The display revision's findings, pending first then newest."""
    from app.models.meeting_dlp import DlpFinding

    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.REDACT)
    rec = await _load_recording(session, workspace_id, recording_id)
    revision_id = transcript_revision_crud.display_revision_id(rec)
    rows = (
        (
            await session.execute(
                select(DlpFinding)
                .where(
                    DlpFinding.recording_id == recording_id,
                    DlpFinding.revision_id == revision_id,
                )
                .order_by(DlpFinding.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [DlpFindingPublic.model_validate(f) for f in rows]


@router.post(
    "/{workspace_id}/recordings/{recording_id}/dlp/scan",
    dependencies=[Depends(ensure_meetings_enabled)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_dlp_scan(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> dict[str, str]:
    """On-demand rescan of the display revision (durable enqueue)."""
    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.REDACT)
    rec = await _load_recording(session, workspace_id, recording_id)
    revision_id = transcript_revision_crud.display_revision_id(rec)
    if revision_id is None:
        raise ConflictError("The recording has no transcript to scan yet")
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.scan_meeting_dlp",
        queue="celery",
        payload={
            "recording_id": str(recording_id),
            "revision_id": str(revision_id),
        },
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"dlp:{revision_id}:manual:{uuid.uuid4().hex[:8]}",
    )
    await session.commit()
    await try_dispatch(session, outbox)
    return {"status": "scanning"}


@router.post(
    "/{workspace_id}/recordings/{recording_id}/dlp/findings",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def add_manual_dlp_finding(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    payload: DlpManualFinding,
    session: SessionDep,
    user: CurrentUserDep,
) -> DlpFindingPublic:
    """Manually mark a span for redaction (created pre-accepted)."""
    from app.models.meeting_dlp import DlpFinding

    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.REDACT)
    await _load_recording(session, workspace_id, recording_id)
    segment = await meeting_recording_crud.get_segment(
        session, recording_id=recording_id, segment_id=payload.segment_id
    )
    if segment is None:
        raise NotFoundError("Segment not found")
    end_char = min(payload.end_char, len(segment.text))
    if payload.start_char >= end_char:
        raise AppError(
            "start_char must be inside the segment text and before end_char",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    finding = DlpFinding(
        workspace_id=workspace_id,
        recording_id=recording_id,
        revision_id=segment.revision_id,
        segment_id=segment.id,
        detector="manual",
        matched_text=segment.text[payload.start_char : end_char][:500],
        start_char=payload.start_char,
        end_char=end_char,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        status="accepted",
    )
    session.add(finding)
    await session.commit()
    await session.refresh(finding)
    return DlpFindingPublic.model_validate(finding)


@router.post(
    "/{workspace_id}/recordings/{recording_id}/dlp/findings/{finding_id}/status",
    dependencies=[Depends(ensure_meetings_enabled)],
)
async def set_dlp_finding_status(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    finding_id: uuid.UUID,
    payload: DlpFindingStatusSet,
    session: SessionDep,
    user: CurrentUserDep,
) -> DlpFindingPublic:
    from app.models.meeting_dlp import DlpFinding

    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.REDACT)
    finding = await session.get(DlpFinding, finding_id)
    if (
        finding is None
        or finding.recording_id != recording_id
        or finding.workspace_id != workspace_id
    ):
        raise NotFoundError("Finding not found")
    if finding.status in ("redacted", "redacting"):
        raise ConflictError("This finding is being redacted or already redacted")
    finding.status = payload.status
    await session.commit()
    await session.refresh(finding)
    return DlpFindingPublic.model_validate(finding)


@router.post(
    "/{workspace_id}/recordings/{recording_id}/redact",
    dependencies=[Depends(ensure_meetings_enabled)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_redaction(
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUserDep,
) -> dict[str, str]:
    """Apply every ACCEPTED finding: superseding masked revision + muted audio
    rendition + object swap (worker-executed, audited, hold-aware)."""
    from app.models.meeting_dlp import DlpFinding

    await _load_authorized(session, user, workspace_id, level="content")
    await _require_cap(session, user, workspace_id, MeetingCapability.REDACT)
    rec = await _load_recording(session, workspace_id, recording_id)
    revision_id = transcript_revision_crud.display_revision_id(rec)
    accepted = (
        (
            await session.execute(
                select(DlpFinding).where(
                    DlpFinding.recording_id == recording_id,
                    DlpFinding.revision_id == revision_id,
                    DlpFinding.status == "accepted",
                )
            )
        )
        .scalars()
        .all()
    )
    if not accepted:
        raise ConflictError("No accepted findings to redact")
    # Deterministic per-revision dedupe (review finding): concurrent apply
    # requests previously each got a random key and ran simultaneously. A
    # successful redaction supersedes the revision (next apply → new key);
    # after a FAILED run the row is stale, so a retry is allowed once the
    # in-flight window has passed.
    from datetime import timedelta

    from app.models.task_outbox import TaskOutbox

    dedupe = f"redact:{recording_id}:{revision_id}"
    existing = (
        (
            await session.execute(
                select(TaskOutbox).where(TaskOutbox.dedupe_key.like(f"{dedupe}%"))
            )
        )
        .scalars()
        .all()
    )
    fresh_cutoff = datetime.now(UTC) - timedelta(seconds=900)
    for row in existing:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if row.status in ("pending", "sent") and created > fresh_cutoff:
            raise ConflictError("A redaction of this revision is already in progress")
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.apply_meeting_redaction",
        queue="transcription",
        payload={"recording_id": str(recording_id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        # uuid suffix (review round 2): two concurrent requests computed the
        # same len() suffix and one hit a raw unique-constraint error. The
        # worker's finding-status CAS is the real concurrency guard now; the
        # fresh-window check above is only UX.
        dedupe_key=f"{dedupe}:{uuid.uuid4().hex[:8]}",
    )
    await meeting_governance_crud.record_audit(
        session,
        action="redaction.apply",
        actor=user,
        workspace_id=workspace_id,
        recording_id=recording_id,
        target={
            "revision_id": str(revision_id),
            "finding_count": len(accepted),
        },
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await try_dispatch(session, outbox)
    return {"status": "redacting", "findings": str(len(accepted))}
