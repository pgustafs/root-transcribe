"""Audio-object lifecycle repository (issue #6).

Async helpers back the API routes (register an object at upload, request its
durable deletion); the sync helpers drive ``tasks.process_audio_deletions``,
the beat sweep that owns every actual store delete and drives each request to a
**confirmed** ``deleted`` or an admin-visible ``delete_failed`` dead letter.
See :mod:`app.models.audio_object`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.audio_object import AUDIO_DELETE_REASONS, AudioObject
from app.models.user import User

# Backoff cap so exponential retry never schedules further than a day out.
_MAX_BACKOFF_SECONDS = 24 * 3600


# --- async (API) ---


async def create(
    session: AsyncSession,
    *,
    bucket: str,
    key: str,
    kind: str,
    ref_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    size_bytes: int | None = None,
    content_type: str | None = None,
) -> AudioObject:
    """Register a freshly stored object (flushed, committed by the caller so the
    row shares its owner's transaction)."""
    obj = AudioObject(
        bucket=bucket,
        key=key,
        kind=kind,
        ref_id=ref_id,
        workspace_id=workspace_id,
        size_bytes=size_bytes,
        content_type=content_type,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get_by_key(session: AsyncSession, key: str) -> AudioObject | None:
    result = await session.execute(select(AudioObject).where(AudioObject.key == key))
    return result.scalars().first()


async def pending_enrollment_samples_for_profile(
    session: AsyncSession, profile_id: uuid.UUID
) -> list[AudioObject]:
    """Active enrollment-sample objects for a profile that have NOT yet produced
    a voiceprint (``ref_id IS NULL``) — i.e. an enrollment queued but not yet
    processed by the worker (issue #14 review).

    Erasure enumerates existing voiceprints, so without this a sample uploaded
    just before the erasure request would be missed and the worker would create
    a voiceprint AFTER the erasure. The enrollment key embeds the profile id
    (``…/speakers/{profile_id}/…``), which is how a not-yet-owned sample is
    attributed back to its subject."""
    result = await session.execute(
        select(AudioObject).where(
            AudioObject.kind == "enrollment_sample",
            AudioObject.ref_id.is_(None),
            AudioObject.status == "active",
            AudioObject.key.contains(f"speakers/{profile_id}/"),
        )
    )
    return list(result.scalars().all())


async def ensure_for_key(
    session: AsyncSession,
    *,
    bucket: str,
    key: str,
    kind: str,
    ref_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> AudioObject:
    """The object row for ``key``, creating it for pre-#6 legacy blobs.

    Race-safe under the unique-key constraint: a concurrent insert loses the
    flush inside a SAVEPOINT and re-reads the winner's row.
    """
    obj = await get_by_key(session, key)
    if obj is not None:
        return obj
    try:
        async with session.begin_nested():
            return await create(
                session,
                bucket=bucket,
                key=key,
                kind=kind,
                ref_id=ref_id,
                workspace_id=workspace_id,
            )
    except IntegrityError:
        obj = await get_by_key(session, key)
        if obj is None:  # pragma: no cover — the winner's row must exist
            raise
        return obj


async def request_delete(
    session: AsyncSession,
    obj: AudioObject,
    *,
    reason: str,
    actor: User | None = None,
    policy: str | None = None,
) -> AudioObject:
    """Ask for durable deletion (idempotent; first request's provenance wins)."""
    _apply_delete_request(obj, reason=reason, actor=actor, policy=policy)
    await session.flush()
    return obj


def apply_rearm(obj: AudioObject, *, reason: str) -> None:
    """Field mutation for :func:`rearm_delete` (pure; no session).

    Re-arm an object for another deletion cycle WITHOUT creating a provenance
    chimera (issue #14 review). The re-arm is a system RETRY of an erasure that
    was already requested — when a late write re-creates a blob whose row the
    pipeline already confirmed deleted — so the ORIGINAL request's provenance
    (reason, requested_at, actor, policy) is PRESERVED and continues to describe
    why the audio is being deleted; only when no prior request exists (a
    never-tombstoned orphan) is the given reason stamped. The accumulated
    ``deleted_version_ids`` and the cumulative ``total_delete_attempts`` are kept
    intact for audit, but the per-cycle ``delete_attempts`` is RESET so the new
    cycle gets the FULL retry budget — otherwise a blob that failed to delete in
    the prior cycle would dead-letter almost immediately in the next (issue #14
    review). Only the transient terminal/retry state is reset.
    """
    if reason not in AUDIO_DELETE_REASONS:  # same closed vocabulary (r15)
        raise ValueError(f"unknown audio delete reason: {reason!r}")
    if obj.delete_requested_at is None:
        obj.delete_reason = reason
        obj.delete_requested_at = datetime.now(UTC)
    obj.status = "delete_requested"
    obj.deleted_at = None  # no longer terminal — a delete is pending again
    obj.last_delete_error = None  # stale error from the prior cycle
    obj.delete_attempts = 0  # fresh per-cycle budget (total_delete_attempts kept)
    obj.next_delete_attempt_at = None  # claimable now


async def rearm_delete(
    session: AsyncSession, obj: AudioObject, *, reason: str
) -> AudioObject:
    """Force an object back to ``delete_requested`` from ANY state — including
    the terminal ``deleted`` / ``delete_failed`` (issue #14 review).

    ``request_delete`` is a no-op once the row leaves ``active`` (first request
    wins), which is correct for normal deletion. But when a blob is written
    AFTER the deletion pipeline already confirmed the row deleted against an
    absent object (the put-vs-delete race), the row must be RE-ARMED so the
    durable sweep deletes the now-existing blob and records real evidence."""
    apply_rearm(obj, reason=reason)
    await session.flush()
    return obj


# --- sync (Celery worker) ---


def get_by_key_sync(session: Session, key: str) -> AudioObject | None:
    return (
        session.execute(select(AudioObject).where(AudioObject.key == key))
        .scalars()
        .first()
    )


def ensure_for_key_sync(
    session: Session,
    *,
    bucket: str,
    key: str,
    kind: str,
    ref_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> AudioObject:
    obj = get_by_key_sync(session, key)
    if obj is not None:
        return obj
    try:
        with session.begin_nested():
            obj = AudioObject(
                bucket=bucket,
                key=key,
                kind=kind,
                ref_id=ref_id,
                workspace_id=workspace_id,
            )
            session.add(obj)
            session.flush()
            return obj
    except IntegrityError:
        obj = get_by_key_sync(session, key)
        if obj is None:  # pragma: no cover — the winner's row must exist
            raise
        return obj


def request_delete_sync(
    session: Session,
    obj: AudioObject,
    *,
    reason: str,
    policy: str | None = None,
) -> None:
    _apply_delete_request(obj, reason=reason, actor=None, policy=policy)
    session.flush()


def claim_due_deletions_sync(
    session: Session,
    *,
    now: datetime,
    limit: int = 200,
    stale_after_seconds: int = 900,
) -> list[AudioObject]:
    """Claim due deletion requests (→ ``deleting``) for one sweep pass.

    Also reclaims rows stuck in ``deleting`` past the stale window — a sweep
    that crashed mid-delete must not strand its claims forever.
    """
    stale_cutoff = now - timedelta(seconds=stale_after_seconds)
    rows = (
        session.execute(
            select(AudioObject)
            .where(
                or_(
                    AudioObject.status == "delete_requested",
                    (AudioObject.status == "deleting")
                    & (AudioObject.updated_at < stale_cutoff),
                )
            )
            .order_by(AudioObject.delete_requested_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    due = [
        obj
        for obj in rows
        if obj.status == "deleting"  # stale reclaim — always due
        or obj.next_delete_attempt_at is None
        or _aware(obj.next_delete_attempt_at) <= now
    ]
    for obj in due:
        obj.status = "deleting"
        obj.updated_at = now
    session.flush()
    return due


def confirm_deleted_sync(
    session: Session,
    obj: AudioObject,
    *,
    now: datetime,
    version_ids: list[str] | None = None,
) -> None:
    """The store confirmed the object is gone — only now may owners claim erasure.

    ``version_ids`` records exactly which object versions were purged on a
    versioned bucket — the erasure evidence (review round 2).
    """
    obj.status = "deleted"
    obj.deleted_at = now
    obj.last_delete_error = None
    if version_ids:
        # UNION, not replace (issue #14 review): an object can be deleted across
        # more than one lifecycle cycle (a late write after a confirmed delete
        # gets re-armed and deleted again). Each cycle's purged version ids must
        # accumulate, or the earlier cycle's erasure evidence is lost.
        existing = list(obj.deleted_version_ids or [])
        obj.deleted_version_ids = existing + [
            v for v in version_ids if v not in existing
        ]
    session.flush()


def record_delete_failure_sync(
    session: Session,
    obj: AudioObject,
    *,
    error: str,
    now: datetime,
    max_attempts: int,
    base_backoff_seconds: int,
) -> bool:
    """Record a failed store delete; returns True when dead-lettered.

    The key is never cleared: a retryable failure goes back to
    ``delete_requested`` with exponential backoff, an exhausted one becomes the
    admin-visible ``delete_failed`` dead letter.
    """
    obj.delete_attempts += 1
    obj.total_delete_attempts = (obj.total_delete_attempts or 0) + 1
    obj.last_delete_error = error[:2000]
    if obj.delete_attempts >= max_attempts:
        obj.status = "delete_failed"
        session.flush()
        return True
    backoff = min(
        base_backoff_seconds * (2 ** (obj.delete_attempts - 1)), _MAX_BACKOFF_SECONDS
    )
    obj.status = "delete_requested"
    obj.next_delete_attempt_at = now + timedelta(seconds=backoff)
    session.flush()
    return False


# --- shared ---


def _apply_delete_request(
    obj: AudioObject, *, reason: str, actor: User | None, policy: str | None
) -> None:
    # The ONE place a reason is stamped, so the closed vocabulary is enforced
    # here rather than trusted at each call site — several of which pass a
    # computed reason that no static check can see (issue #37 review r15).
    if reason not in AUDIO_DELETE_REASONS:
        raise ValueError(f"unknown audio delete reason: {reason!r}")
    if obj.status != "active":
        return  # already requested / in flight / terminal — first request wins
    obj.status = "delete_requested"
    obj.delete_reason = reason
    obj.delete_policy = policy
    obj.delete_requested_at = datetime.now(UTC)
    obj.next_delete_attempt_at = None
    if actor is not None:
        obj.delete_requested_by_id = actor.id
        obj.delete_requested_by_email = actor.email


def _aware(value: datetime) -> datetime:
    """SQLite round-trips DateTime(timezone=True) as naive UTC — normalize."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
