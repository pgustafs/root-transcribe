"""Speaker directory routes — enrolled people + voiceprints for identification.

Two audiences:

- **Everyone (authenticated)** may *read* the directory (names only) so the meeting
  transcript UI can offer a speaker typeahead, and may **self-enroll** their own
  voice (``/speakers/me/...``) with explicit consent, and exercise their right to
  erasure (``DELETE /speakers/me/voiceprints``).
- **Privileged users (admin / power user)** manage the directory: create/rename/
  deactivate entries and enroll or delete voiceprints for any person.

Voiceprints are biometric data: the API never returns the embedding, enrollment
requires recorded consent, and the actual embedding is computed + encrypted in the
``transcription``-queue worker. See ARCHITECTURE.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from app.api.deps import AdminDep, CurrentUserDep, PrivilegedDep
from app.core.config import settings
from app.core.exceptions import AppError, NotFoundError
from app.core.logging import request_id_ctx
from app.crud import audio_object as audio_object_crud
from app.crud import speaker_consent as speaker_consent_crud
from app.crud import speaker_profile as speaker_profile_crud
from app.crud import task_outbox as task_outbox_crud
from app.db.session import SessionDep
from app.models.speaker_profile import SpeakerProfile
from app.schemas.speaker import (
    ComplianceExportCreate,
    ComplianceExportPublic,
    SpeakerConsentCreate,
    SpeakerConsentPublic,
    SpeakerConsentWithdraw,
    SpeakerProfileCreate,
    SpeakerProfilePublic,
    SpeakerProfileUpdate,
)
from app.services.audio_store import get_audio_store, new_voiceprint_key


def ensure_voice_profiles_enabled() -> None:
    """Enable speaker enrollment when transcription and audio storage are enabled."""
    if not settings.MEETINGS_ENABLED:
        raise AppError(
            "Voice profiles are not enabled on this deployment",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not settings.AUDIO_S3_ENDPOINT_URL:
        raise AppError(
            "Voice sample storage is not configured",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


router = APIRouter(
    prefix="/speakers",
    tags=["speakers"],
    dependencies=[Depends(ensure_voice_profiles_enabled)],
)

# Data-subject rights and consent obligations OUTLIVE the processing feature
# (issue #14 review): erasure, consent withdrawal, DSAR read/export must work
# even when meeting processing is switched off, or when audio storage is
# unconfigured — the biometric data and the consent record still exist, so the
# obligation still binds. This router is DELIBERATELY not feature-gated; put
# ONLY rights/withdrawal/erasure/DSAR endpoints on it (never enrollment or any
# new processing).
rights_router = APIRouter(prefix="/speakers", tags=["speakers"])

# Enrollment samples are short; cap them well below the meeting-audio ceiling.
_SAMPLE_MAX_BYTES = 25 * 1024 * 1024


def _public(profile: SpeakerProfile, count: int) -> SpeakerProfilePublic:
    item = SpeakerProfilePublic.model_validate(profile)
    item.voiceprint_count = count
    return item


@rights_router.get("")
async def list_speakers(
    session: SessionDep, _user: CurrentUserDep
) -> list[SpeakerProfilePublic]:
    """The directory (names + counts) — readable by any authenticated user."""
    rows = await speaker_profile_crud.list_all(session)
    return [_public(profile, count) for profile, count in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_speaker(
    payload: SpeakerProfileCreate, session: SessionDep, user: PrivilegedDep
) -> SpeakerProfilePublic:
    """Create a directory entry (admin / power user).

    Asserting consent for ANOTHER person is an elevated act (issue #6): it
    requires the ADMIN role plus recorded evidence of how the consent was
    obtained, and lands in the immutable consent ledger.
    """
    on_behalf = payload.user_id != user.id
    if payload.consent_granted and on_behalf:
        from app.models.user import Role

        if user.role != Role.ADMIN:
            raise AppError(
                "Only an administrator may assert consent for another person",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        if not (payload.consent_evidence or "").strip():
            raise AppError(
                "Recording consent for another person requires evidence",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    profile = await speaker_profile_crud.create(
        session,
        display_name=payload.display_name,
        user_id=payload.user_id,
        consent_granted=payload.consent_granted,
        actor=user,
    )
    if payload.consent_granted:
        await speaker_consent_crud.record(
            session,
            profile_id=profile.id,
            purpose="speaker_identification",
            status="granted",
            policy_version=settings.CONSENT_POLICY_VERSION,
            actor=user,
            on_behalf=on_behalf,
            evidence=(
                {"note": payload.consent_evidence} if payload.consent_evidence else None
            ),
        )
        await session.commit()
    return _public(profile, 0)


@rights_router.get("/me")
async def get_my_speaker(
    session: SessionDep, user: CurrentUserDep
) -> SpeakerProfilePublic | None:
    """The caller's own directory entry, if they have self-enrolled.

    A DSAR-grade read (ungated): a data subject may always see what biometric
    data exists about them, so the erasure/withdrawal UI stays usable even
    after the processing feature is switched off (issue #14 review)."""
    profile = await speaker_profile_crud.get_for_user(session, user.id)
    if profile is None:
        return None
    count = await speaker_profile_crud.count_voiceprints(session, profile.id)
    item = _public(profile, count)
    item.pending_enrollment_count = len(
        await audio_object_crud.pending_enrollment_samples_for_profile(
            session, profile.id
        )
    )
    return item


async def _read_sample(file: UploadFile) -> bytes:
    """Read an enrollment sample with the size cap enforced DURING the read
    (review finding: read()-then-check buffered unbounded uploads first)."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > _SAMPLE_MAX_BYTES:
            raise AppError(
                "Voice sample is too large",
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _finalize_enrollment_upload(
    session: SessionDep, profile_id, key, *, filename, user_email
) -> bool:
    """Post-put authoritative recheck (issue #14 review). Returns True if the
    enrollment was enqueued, False if a concurrent event won.

    Under the profile lock (which erasure/withdrawal/deactivation also take),
    re-verify EVERYTHING the enrollment depends on — the profile still exists
    and is active, identification consent is live, and the sample's lifecycle
    row is active. If any fails (a whole-profile erasure leaves NO profile row;
    the put-vs-delete race leaves the row terminal ``deleted``), the just-written
    blob is DURABLY re-armed for deletion so the sweep removes it with real
    evidence — never a suppressed best-effort delete that would silently leak
    biometric audio — and nothing is enqueued."""
    from app.crud import task_outbox as task_outbox_crud
    from app.services.task_outbox import try_dispatch

    profile = await _lock_profile(session, profile_id)
    obj = await audio_object_crud.get_by_key(session, key)
    consent = await speaker_consent_crud.current_status(
        session, profile_id=profile_id, purpose="speaker_identification"
    )
    ok = (
        profile is not None
        and profile.status == "active"
        and consent == "granted"
        and obj is not None
        and obj.status == "active"
    )
    if not ok:
        # Durably re-arm deletion (get-or-create the row, force delete_requested
        # even from a terminal state) and retire any outbox row. The durable
        # deletion sweep — NOT a fast-path delete here — performs the store
        # delete: it is the single place that records complete version evidence
        # (issue #14 review), so doing an inline delete would discard the purged
        # version id and split the audit across two code paths.
        reclaim = await audio_object_crud.ensure_for_key(
            session,
            bucket=settings.AUDIO_S3_BUCKET,
            key=key,
            kind="enrollment_sample",
        )
        await audio_object_crud.rearm_delete(
            session, reclaim, reason="enrollment_cancelled_race"
        )
        await task_outbox_crud.consume(session, dedupe_key=f"enroll:{key}")
        await session.commit()
        return False
    # Durable enqueue: the outbox row commits WITH the sample registration
    # (still under the profile lock, so a concurrent erasure serializes after
    # it); the dispatcher re-sends on a broker outage.
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.enroll_voiceprint",
        queue="transcription",
        payload={
            "profile_id": str(profile_id),
            "sample_key": key,
            "filename": filename,
        },
        headers={"request_id": request_id_ctx.get(), "user": user_email},
        dedupe_key=f"enroll:{key}",
    )
    await session.commit()
    await try_dispatch(session, outbox)
    return True


async def _enqueue_enrollment(
    session: SessionDep,
    profile_id: uuid.UUID,
    data: bytes,
    filename: str,
    content_type: str,
    user_email: str,
) -> bool:
    """Register + store an enrollment sample and enqueue processing. Returns
    False if a concurrent erasure/deactivation cancelled it during the upload."""
    if not data:
        raise AppError("Empty audio sample", status_code=status.HTTP_400_BAD_REQUEST)
    key = new_voiceprint_key(profile_id, filename)
    store = get_audio_store()
    # Intent-first (review round 2): the lifecycle row commits BEFORE the
    # store write, so a crash mid-put can never leave untracked biometric
    # audio; a failed put tombstones the row. The worker re-points ref_id at
    # the voiceprint it produces (that stamp doubles as the idempotency
    # marker for outbox redelivery).
    intent = await audio_object_crud.create(
        session,
        bucket=settings.AUDIO_S3_BUCKET,
        key=key,
        kind="enrollment_sample",
        size_bytes=len(data),
        content_type=content_type,
    )
    await session.commit()
    try:
        await anyio.to_thread.run_sync(store.put, key, data, content_type)
    except Exception:
        await audio_object_crud.request_delete(
            session, intent, reason="enrollment_upload_failed"
        )
        await session.commit()
        raise
    return await _finalize_enrollment_upload(
        session, profile_id, key, filename=filename, user_email=user_email
    )


@router.post("/me/voiceprints", status_code=status.HTTP_202_ACCEPTED)
async def enroll_my_voiceprint(
    session: SessionDep,
    user: CurrentUserDep,
    file: Annotated[UploadFile, File()],
    consent_granted: Annotated[bool, Form()] = False,
) -> SpeakerProfilePublic:
    """Self-enroll a voice sample (creates the caller's entry on first use).

    Requires explicit consent — either already recorded on the profile or granted
    now via the ``consent_granted`` flag.
    """
    profile = await speaker_profile_crud.get_for_user(session, user.id)
    if profile is None:
        profile = await speaker_profile_crud.create(
            session,
            display_name=user.full_name or user.email,
            user_id=user.id,
            consent_granted=consent_granted,
            actor=user,
        )
    # Reject a DEACTIVATED profile BEFORE reading/storing the biometric upload
    # (issue #14 review): data minimization — a revoked person's raw sample must
    # not be written to storage only for the worker to delete it later.
    if profile.status != "active":
        raise AppError(
            "This voice profile is deactivated — reactivate it before enrolling",
            status_code=status.HTTP_409_CONFLICT,
        )
    # The consent LEDGER is authoritative (review finding): the legacy
    # ``consent_granted_at`` timestamp neither expires nor withdraws, so the
    # gate reads the latest ledger state instead.
    ledger = await speaker_consent_crud.current_status(
        session, profile_id=profile.id, purpose="speaker_identification"
    )
    if ledger != "granted":
        if not consent_granted:
            raise AppError(
                "Voice enrollment requires explicit consent",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        profile.consent_granted_at = datetime.now(UTC)
        await speaker_consent_crud.record(
            session,
            profile_id=profile.id,
            purpose="speaker_identification",
            status="granted",
            policy_version=settings.CONSENT_POLICY_VERSION,
            actor=user,
            on_behalf=False,
        )
        await session.commit()
    data = await _read_sample(file)
    enqueued = await _enqueue_enrollment(
        session,
        profile.id,
        data,
        file.filename or "sample.webm",
        file.content_type or "audio/webm",
        user.email,
    )
    if not enqueued:
        # A concurrent erasure, profile deactivation, or consent
        # withdrawal/expiry cancelled this enrollment during the upload (issue
        # #14 review) — report it honestly, not a false 202.
        raise AppError(
            "Enrollment was cancelled during upload by a concurrent erasure, "
            "profile deactivation, or consent withdrawal/expiry",
            status_code=status.HTTP_409_CONFLICT,
        )
    count = await speaker_profile_crud.count_voiceprints(session, profile.id)
    return _public(profile, count)


async def _load_profile(session: SessionDep, profile_id: uuid.UUID) -> SpeakerProfile:
    profile = await speaker_profile_crud.get(session, profile_id)
    if profile is None:
        raise NotFoundError("Speaker not found")
    return profile


@router.patch("/{profile_id}")
async def update_speaker(
    profile_id: uuid.UUID,
    payload: SpeakerProfileUpdate,
    session: SessionDep,
    _user: PrivilegedDep,
) -> SpeakerProfilePublic:
    """Rename or (de)activate a directory entry (admin / power user)."""
    profile = await _load_profile(session, profile_id)
    profile = await speaker_profile_crud.update(
        session, profile, display_name=payload.display_name, status=payload.status
    )
    count = await speaker_profile_crud.count_voiceprints(session, profile.id)
    return _public(profile, count)


@rights_router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_speaker(
    profile_id: uuid.UUID, session: SessionDep, user: PrivilegedDep
) -> None:
    """Delete a directory entry and all its voiceprints (admin / power user).

    Sample audio gets durable deletion tombstones (issue #6) — the rows cascade
    away now, the blobs are driven to confirmed deletion by the worker sweep.
    """
    profile = await _load_profile(session, profile_id)
    # Take the profile lock FIRST (issue #14 review): the tombstone loop below
    # locks AudioObject rows, and every other path locks profile→object — so
    # acquiring the profile lock only implicitly at delete_profile() would give
    # this endpoint the opposite (object→profile) order and risk a deadlock.
    await _lock_profile(session, profile_id)
    for vp in await speaker_profile_crud.list_voiceprints(session, profile.id):
        await _request_sample_delete(session, vp, reason="profile_delete", actor=user)
    # Tombstone queued-but-unprocessed enrollment samples too (issue #14 review)
    # — the profile CASCADE removes voiceprints but not the sample objects, and
    # erasure must durably request their deletion, not rely on the worker.
    await _tombstone_pending_enrollment_samples(session, profile_id, actor=user)
    await speaker_profile_crud.delete_profile(session, profile)


@router.post("/{profile_id}/voiceprints", status_code=status.HTTP_202_ACCEPTED)
async def enroll_voiceprint_for(
    profile_id: uuid.UUID,
    session: SessionDep,
    user: PrivilegedDep,
    file: Annotated[UploadFile, File()],
) -> SpeakerProfilePublic:
    """Enroll a voice sample for any profile (admin / power user).

    The profile must already carry consent (set at creation or via update).
    """
    profile = await _load_profile(session, profile_id)
    # Reject a DEACTIVATED profile before reading/storing the upload (issue #14
    # review) — data minimization at the boundary, not just in the worker.
    if profile.status != "active":
        raise AppError(
            "This voice profile is deactivated — reactivate it before enrolling",
            status_code=status.HTTP_409_CONFLICT,
        )
    ledger = await speaker_consent_crud.current_status(
        session, profile_id=profile.id, purpose="speaker_identification"
    )
    if ledger != "granted":
        raise AppError(
            "This person has no live speaker-identification consent "
            "(never granted, withdrawn, or expired)",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    data = await _read_sample(file)
    enqueued = await _enqueue_enrollment(
        session,
        profile.id,
        data,
        file.filename or "sample.webm",
        file.content_type or "audio/webm",
        user.email,
    )
    if not enqueued:
        # A concurrent erasure, profile deactivation, or consent
        # withdrawal/expiry cancelled this enrollment during the upload (issue
        # #14 review) — report it honestly, not a false 202.
        raise AppError(
            "Enrollment was cancelled during upload by a concurrent erasure, "
            "profile deactivation, or consent withdrawal/expiry",
            status_code=status.HTTP_409_CONFLICT,
        )
    count = await speaker_profile_crud.count_voiceprints(session, profile.id)
    return _public(profile, count)


@rights_router.delete("/me/voiceprints", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_voiceprints(session: SessionDep, user: CurrentUserDep) -> None:
    """Right to erasure: delete all of the caller's own voiceprints.

    The embedding rows are erased immediately; sample audio gets durable
    deletion tombstones driven to confirmation by the worker sweep.
    """
    profile = await speaker_profile_crud.get_for_user(session, user.id)
    if profile is None:
        return
    # Serialize on the profile lock (issue #14 review): the universal boundary.
    # Hold it through ONE commit (no per-item commit) so a queued enrollment
    # worker cannot insert a new voiceprint mid-erasure, and tombstone any
    # PENDING (queued-but-unprocessed) sample so the worker refuses to create
    # one afterward.
    await _lock_profile(session, profile.id)
    for vp in await speaker_profile_crud.list_voiceprints(session, profile.id):
        await _request_sample_delete(session, vp, reason="user_request", actor=user)
        await speaker_profile_crud.delete_voiceprint(
            session, profile_id=profile.id, voiceprint_id=vp.id, commit=False
        )
    await _tombstone_pending_enrollment_samples(session, profile.id, actor=user)
    await session.commit()


async def _request_sample_delete(
    session: SessionDep, vp, *, reason: str, actor
) -> None:
    """Tombstone a voiceprint's enrollment-sample object for durable deletion."""
    if not vp.sample_key:
        return
    obj = await audio_object_crud.ensure_for_key(
        session,
        bucket=settings.AUDIO_S3_BUCKET,
        key=vp.sample_key,
        kind="enrollment_sample",
        ref_id=vp.id,
    )
    await audio_object_crud.request_delete(session, obj, reason=reason, actor=actor)


async def _tombstone_pending_enrollment_samples(
    session: SessionDep, profile_id, *, actor
) -> None:
    """Tombstone enrollment samples QUEUED but not yet turned into a voiceprint
    (issue #14 review). Erasure enumerates existing voiceprints, so a sample
    uploaded just before the erasure request would otherwise survive and the
    worker would create a voiceprint AFTER erasure. Tombstoning under the
    profile lock means the worker's claim sees a non-active sample and refuses
    to enroll. Caller holds the profile lock and commits."""
    from app.crud import task_outbox as task_outbox_crud

    pending = await audio_object_crud.pending_enrollment_samples_for_profile(
        session, profile_id
    )
    for obj in pending:
        await audio_object_crud.request_delete(
            session, obj, reason="erasure_pending_enrollment", actor=actor
        )
        # Retire the cancelled enrollment's outbox row in the SAME transaction
        # (issue #14 review) — otherwise the dispatcher keeps re-sending erased
        # work through a broker outage.
        await task_outbox_crud.consume(session, dedupe_key=f"enroll:{obj.key}")


async def _lock_profile(session: SessionDep, profile_id) -> SpeakerProfile | None:
    """Take a FOR UPDATE lock on the profile row and RETURN it (None if it was
    deleted) — the single serialization boundary for consent-lifecycle changes
    (issue #14 review).

    Acquired at the START of every consent mutation, BEFORE any consent row is
    written, so a grant, a withdrawal, and the enrollment/expiry sweeps that
    also take this one lock cannot interleave into "consent withdrawn but
    sample retained" (or the reverse). Returning the row lets callers see that a
    concurrent whole-profile erasure won (no row) instead of silently
    continuing. A no-op on SQLite, which serializes writes anyway.
    """
    from sqlalchemy import select as _select

    from app.models.speaker_profile import SpeakerProfile as _SP

    result = await session.execute(
        _select(_SP)
        .where(_SP.id == profile_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def _purge_retained_samples(session: SessionDep, profile, *, actor) -> None:
    """Stop retaining a profile's raw samples when the justifying consent ends.

    Withdrawal must stop retention NOW, not only block future use (issue #14
    review) — but it only *requests* durable deletion. It must NOT clear
    ``sample_key``: the established invariant is that application metadata may
    claim audio is gone only after the object store CONFIRMS the delete
    (``_finalize_confirmed_deletion`` nulls the key then, and DSAR deletion
    evidence is found via the still-set key). The audio lifecycle records the
    pending deletion while storage retries run; erasure evidence must distinguish
    a deletion request from confirmed removal.

    The caller holds the profile lock (see :func:`_lock_profile`).
    """
    for vp in await speaker_profile_crud.list_voiceprints(session, profile.id):
        if vp.sample_key:
            await _request_sample_delete(
                session, vp, reason="consent_withdrawn", actor=actor
            )


async def _record_coupled_consent(
    session: SessionDep,
    *,
    profile,
    purpose: str,
    new_status: str,
    actor,
    on_behalf: bool,
    expires_at=None,
    withdrawal_reason: str | None = None,
    evidence: dict | None = None,
):
    """Record supported consent changes, preserving historical withdrawal rules.

    New grants cover identification and sample retention only. Existing synthesis
    grants remain withdrawable and coupled to sample retention so old samples
    are still purged when either historical purpose is withdrawn.
    """
    # Serialize the WHOLE consent change on the profile lock, before recording.
    await _lock_profile(session, profile.id)

    companions: list[str] = []
    if purpose == "voice_synthesis" and new_status == "withdrawn":
        companions.append("sample_retention")
    elif purpose == "sample_retention" and new_status == "withdrawn":
        companions.append("voice_synthesis")

    row = await speaker_consent_crud.record(
        session,
        profile_id=profile.id,
        purpose=purpose,
        status=new_status,
        policy_version=settings.CONSENT_POLICY_VERSION,
        actor=actor,
        on_behalf=on_behalf,
        expires_at=expires_at if new_status == "granted" else None,
        withdrawal_reason=withdrawal_reason if new_status == "withdrawn" else None,
        evidence=evidence,
    )
    for companion in companions:
        await speaker_consent_crud.record(
            session,
            profile_id=profile.id,
            purpose=companion,
            status=new_status,
            policy_version=settings.CONSENT_POLICY_VERSION,
            actor=actor,
            on_behalf=on_behalf,
            expires_at=expires_at if new_status == "granted" else None,
            withdrawal_reason=withdrawal_reason if new_status == "withdrawn" else None,
            evidence=evidence,
        )
    if new_status == "withdrawn" and purpose in ("voice_synthesis", "sample_retention"):
        await _purge_retained_samples(session, profile, actor=actor)
    return row


# --- Consent ledger (issue #6) ----------------------------------------------


@rights_router.get("/me/consents")
async def list_my_consents(
    session: SessionDep, user: CurrentUserDep
) -> list[SpeakerConsentPublic]:
    """The caller's own consent ledger (DSAR-grade history)."""
    profile = await speaker_profile_crud.get_for_user(session, user.id)
    if profile is None:
        return []
    rows = await speaker_consent_crud.list_for_profile(session, profile.id)
    return [SpeakerConsentPublic.model_validate(r) for r in rows]


@router.post("/me/consents", status_code=status.HTTP_201_CREATED)
async def grant_my_consent(
    payload: SpeakerConsentCreate, session: SessionDep, user: CurrentUserDep
) -> SpeakerConsentPublic:
    """Grant consent for one purpose (self-service; immutable ledger row)."""
    profile = await speaker_profile_crud.get_for_user(session, user.id)
    if profile is None:
        profile = await speaker_profile_crud.create(
            session,
            display_name=user.full_name or user.email,
            user_id=user.id,
            consent_granted=False,
            actor=user,
        )
    row = await _record_coupled_consent(
        session,
        profile=profile,
        purpose=payload.purpose,
        new_status="granted",
        actor=user,
        on_behalf=False,
        expires_at=payload.expires_at,
    )
    if payload.purpose == "speaker_identification":
        profile.consent_granted_at = datetime.now(UTC)
    await session.commit()
    return SpeakerConsentPublic.model_validate(row)


@rights_router.post("/me/consents/withdraw", status_code=status.HTTP_201_CREATED)
async def withdraw_my_consent(
    payload: SpeakerConsentWithdraw, session: SessionDep, user: CurrentUserDep
) -> SpeakerConsentPublic:
    """Withdraw consent for one purpose — excluded from matching immediately."""
    profile = await speaker_profile_crud.get_for_user(session, user.id)
    if profile is None:
        raise NotFoundError("You have no speaker profile")
    row = await _record_coupled_consent(
        session,
        profile=profile,
        purpose=payload.purpose,
        new_status="withdrawn",
        actor=user,
        on_behalf=False,
        withdrawal_reason=payload.reason,
    )
    if payload.purpose == "speaker_identification":
        profile.consent_granted_at = None
    await session.commit()
    return SpeakerConsentPublic.model_validate(row)


@rights_router.get("/{profile_id}/consents")
async def list_profile_consents(
    profile_id: uuid.UUID, session: SessionDep, _user: PrivilegedDep
) -> list[SpeakerConsentPublic]:
    """A profile's consent ledger (admin / power user)."""
    await _load_profile(session, profile_id)
    rows = await speaker_consent_crud.list_for_profile(session, profile_id)
    return [SpeakerConsentPublic.model_validate(r) for r in rows]


@router.post("/{profile_id}/consents", status_code=status.HTTP_201_CREATED)
async def grant_profile_consent(
    profile_id: uuid.UUID,
    payload: SpeakerConsentCreate,
    session: SessionDep,
    user: AdminDep,
) -> SpeakerConsentPublic:
    """Record consent on behalf of another person (ADMIN + evidence required)."""
    profile = await _load_profile(session, profile_id)
    if not (payload.evidence or "").strip():
        raise AppError(
            "Recording consent for another person requires evidence",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    row = await _record_coupled_consent(
        session,
        profile=profile,
        purpose=payload.purpose,
        new_status="granted",
        actor=user,
        on_behalf=True,
        evidence={"note": payload.evidence},
        expires_at=payload.expires_at,
    )
    if payload.purpose == "speaker_identification":
        profile.consent_granted_at = datetime.now(UTC)
    await session.commit()
    return SpeakerConsentPublic.model_validate(row)


@rights_router.post(
    "/{profile_id}/consents/withdraw", status_code=status.HTTP_201_CREATED
)
async def withdraw_profile_consent(
    profile_id: uuid.UUID,
    payload: SpeakerConsentWithdraw,
    session: SessionDep,
    user: PrivilegedDep,
) -> SpeakerConsentPublic:
    """Withdraw a profile's consent for one purpose (admin / power user)."""
    profile = await _load_profile(session, profile_id)
    row = await _record_coupled_consent(
        session,
        profile=profile,
        purpose=payload.purpose,
        new_status="withdrawn",
        actor=user,
        on_behalf=True,
        withdrawal_reason=payload.reason,
    )
    if payload.purpose == "speaker_identification":
        profile.consent_granted_at = None
    await session.commit()
    return SpeakerConsentPublic.model_validate(row)


# --- DSAR / compliance exports (issue #6, wave E5; ADMIN only) --------------


@rights_router.post("/compliance-exports", status_code=201)
async def request_compliance_export(
    payload: ComplianceExportCreate,
    session: SessionDep,
    user: AdminDep,
) -> ComplianceExportPublic:
    """Start a DSAR bundle build (durable enqueue, audited)."""
    from sqlalchemy import select as sa_select

    from app.core.logging import request_id_ctx
    from app.crud import meeting_governance as meeting_governance_crud
    from app.models.compliance_export import ComplianceExportJob
    from app.models.speaker_profile import SpeakerProfile as ProfileModel
    from app.services.task_outbox import try_dispatch

    if payload.speaker_profile_id is not None:
        profile = (
            await session.execute(
                sa_select(ProfileModel).where(
                    ProfileModel.id == payload.speaker_profile_id
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            raise NotFoundError("Speaker profile not found")
    job = ComplianceExportJob(
        subject_email=payload.subject_email.strip().lower(),
        speaker_profile_id=payload.speaker_profile_id,
        requested_by_id=user.id,
        requested_by_email=user.email,
    )
    session.add(job)
    await session.flush()
    outbox = await task_outbox_crud.enqueue(
        session,
        task_name="tasks.build_compliance_export",
        queue="celery",
        payload={"job_id": str(job.id)},
        headers={"request_id": request_id_ctx.get(), "user": user.email},
        dedupe_key=f"dsar:{job.id}",
    )
    await meeting_governance_crud.record_audit(
        session,
        action="compliance.export_requested",
        actor=user,
        target={"job_id": str(job.id), "subject": job.subject_email},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    await try_dispatch(session, outbox)
    await session.refresh(job)
    return ComplianceExportPublic.model_validate(job)


@rights_router.get("/compliance-exports")
async def list_compliance_exports(
    session: SessionDep, _user: AdminDep
) -> list[ComplianceExportPublic]:
    from sqlalchemy import select as sa_select

    from app.models.compliance_export import ComplianceExportJob

    rows = (
        (
            await session.execute(
                sa_select(ComplianceExportJob).order_by(
                    ComplianceExportJob.created_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    return [ComplianceExportPublic.model_validate(j) for j in rows]


@rights_router.get("/compliance-exports/{job_id}/download")
async def download_compliance_export(
    job_id: uuid.UUID,
    session: SessionDep,
    user: AdminDep,
):
    """Stream a completed bundle (audited download)."""
    from fastapi.responses import StreamingResponse

    from app.core.logging import request_id_ctx
    from app.crud import meeting_governance as meeting_governance_crud
    from app.models.compliance_export import ComplianceExportJob

    job = await session.get(ComplianceExportJob, job_id)
    if job is None or job.status != "completed" or not job.audio_key:
        raise NotFoundError("Export bundle is not available")
    store = get_audio_store()
    chunks = await anyio.to_thread.run_sync(store.stream_sync, job.audio_key)
    await meeting_governance_crud.record_audit(
        session,
        action="compliance.export_downloaded",
        actor=user,
        target={"job_id": str(job.id), "subject": job.subject_email},
        request_id=request_id_ctx.get(),
    )
    await session.commit()
    return StreamingResponse(
        chunks,
        media_type="application/zip",
        headers={
            "Content-Disposition": (f'attachment; filename="dsar-{job.id.hex}.zip"'),
            **({"Content-Length": str(job.size_bytes)} if job.size_bytes else {}),
        },
    )
