"""Resumable upload-session repository (issue #6, wave B1)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.meeting_upload_session import (
    LIVE_UPLOAD_SESSION_STATUSES,
    MeetingUploadPart,
    MeetingUploadSession,
)
from app.models.user import User


async def create_session(
    session: AsyncSession,
    *,
    recording_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: str,
    s3_upload_id: str | None,
    filename: str,
    content_type: str,
    size_bytes: int | None,
    actor: User,
) -> MeetingUploadSession:
    row = MeetingUploadSession(
        recording_id=recording_id,
        workspace_id=workspace_id,
        key=key,
        s3_upload_id=s3_upload_id,
        part_size_bytes=settings.MEETING_UPLOAD_PART_MB * 1024 * 1024,
        declared_size_bytes=size_bytes,
        filename=filename,
        content_type=content_type,
        expires_at=datetime.now(UTC)
        + timedelta(hours=settings.MEETING_UPLOAD_SESSION_TTL_HOURS),
        created_by_id=actor.id,
        created_by_email=actor.email,
    )
    session.add(row)
    await session.flush()
    return row


async def get_session(
    session: AsyncSession, *, workspace_id: uuid.UUID, session_id: uuid.UUID
) -> MeetingUploadSession | None:
    result = await session.execute(
        select(MeetingUploadSession).where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.workspace_id == workspace_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert_part(
    session: AsyncSession,
    *,
    upload_session: MeetingUploadSession,
    part_number: int,
    size_bytes: int,
    sha256: str,
    etag: str,
) -> MeetingUploadPart:
    """Record one uploaded part (idempotent: a retried part overwrites)."""
    result = await session.execute(
        select(MeetingUploadPart).where(
            MeetingUploadPart.session_id == upload_session.id,
            MeetingUploadPart.part_number == part_number,
        )
    )
    part = result.scalar_one_or_none()
    if part is None:
        part = MeetingUploadPart(session_id=upload_session.id, part_number=part_number)
        session.add(part)
    part.size_bytes = size_bytes
    part.sha256 = sha256
    part.etag = etag
    part.uploaded_at = datetime.now(UTC)
    await session.flush()
    return part


async def list_parts(
    session: AsyncSession, *, session_id: uuid.UUID
) -> Sequence[MeetingUploadPart]:
    result = await session.execute(
        select(MeetingUploadPart)
        .where(MeetingUploadPart.session_id == session_id)
        .order_by(MeetingUploadPart.part_number)
    )
    return result.scalars().all()


def list_expired_sync(
    session: Session, *, now: datetime, lease_cutoff: datetime | None = None
) -> list[MeetingUploadSession]:
    """Live sessions past their expiry (the abandoned-upload sweep).

    A session whose completion lease is still FRESH is excluded: expiry and a
    legitimate in-flight completion can otherwise coincide near the TTL
    boundary, and the sweep would abort the multipart upload out from under it
    (issue #37 review round 11).

    Every LEASE state is listed once its claim has gone stale — ``expiring``
    (a sweep that died mid-reconciliation) and ``cancelling`` (a cancel whose
    abort never returned) included. The sweep is the last-resort reaper: a
    state only it can reach would strand the session outside every future
    sweep (issue #37 review rounds 12-13).
    """
    cutoff = lease_cutoff if lease_cutoff is not None else now
    return list(
        session.execute(
            select(MeetingUploadSession).where(
                MeetingUploadSession.status.in_(LIVE_UPLOAD_SESSION_STATUSES),
                MeetingUploadSession.expires_at < now,
                or_(
                    MeetingUploadSession.status == "active",
                    MeetingUploadSession.completing_at.is_(None),
                    MeetingUploadSession.completing_at < cutoff,
                ),
            )
        )
        .scalars()
        .all()
    )


async def claim_for_completion(
    session: AsyncSession, *, session_id: uuid.UUID, stale_before: datetime
) -> datetime | None:
    """Atomically LEASE the completion of an upload session.

    One conditional UPDATE covers both cases (issue #37 review rounds 9-10):

    - ``active`` — the normal first claim. A plain read-then-write let two
      concurrent requests both observe ``active`` and both run the external
      multipart completion.
    - ``completing`` whose lease has EXPIRED — the previous owner died or its
      storage call failed. Without reclaim the row stays ``completing`` forever
      and every retry is told "still in flight": a permanent false success, and
      an orphaned object if the store had already accepted it.

    Returns the lease token (the timestamp written) or ``None`` if another
    caller holds it. The token FENCES the finalize: a holder whose lease was
    reclaimed mid-flight — the storage calls can outlast it — must not write
    recording state on top of the new owner (issue #37 review round 11).
    """
    now = datetime.now(UTC)
    result = await session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            or_(
                MeetingUploadSession.status == "active",
                and_(
                    MeetingUploadSession.status == "completing",
                    or_(
                        MeetingUploadSession.completing_at.is_(None),
                        MeetingUploadSession.completing_at < stale_before,
                    ),
                ),
            ),
        )
        .values(status="completing", completing_at=now)
        # Straight to SQL: the ORM's Python-side synchronization would try to
        # evaluate the timestamp comparison itself, and SQLite hands back naive
        # datetimes it cannot compare with an aware bound value.
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return now if result.rowcount == 1 else None


async def finalize_completion(
    session: AsyncSession, *, session_id: uuid.UUID, lease: datetime
) -> bool:
    """Flip ``completing`` → ``completed``, but ONLY if we still hold ``lease``.

    The fence (issue #37 review round 11): the external storage calls can take
    longer than the lease, so by the time a holder finishes, another caller may
    legitimately have reclaimed and be finishing too. Whoever still owns the
    lease commits; the other abandons instead of writing over it.
    """
    result = await session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.status == "completing",
            MeetingUploadSession.completing_at == lease,
        )
        .values(status="completed")
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def claim_for_expiry_sync(
    session: Session,
    *,
    session_id: uuid.UUID,
    now: datetime,
    lease_cutoff: datetime,
) -> datetime | None:
    """Atomically CLAIM an expired session for the sweep, before touching storage.

    Filtering fresh leases in the sweep's SELECT is not enough (issue #37 review
    round 12): between that read and the abort, a client can reclaim the stale
    lease and start a legitimate completion — and the sweep would then abort the
    multipart upload and delete the recording underneath it. The completion
    fence cannot defend against that, because the sweep destroys STORAGE, not
    just rows.

    So the sweep takes the row the same way a completer does: a conditional
    UPDATE that only succeeds while the session is still expired-and-unleased.
    A reclaim that lands first flips ``completing_at`` and this returns ``None``.

    ``now`` must be stamped PER ROW, not once per sweep: a long sweep would
    otherwise hand its later rows a lease that is already stale, and the
    terminal write's fence would fail for no reason (review round 13).

    Returns the lease token, which FENCES the sweep's terminal write the same
    way a completion's token fences its finalize.
    """
    result = session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.status.in_(LIVE_UPLOAD_SESSION_STATUSES),
            MeetingUploadSession.expires_at < now,
            or_(
                MeetingUploadSession.status == "active",
                MeetingUploadSession.completing_at.is_(None),
                MeetingUploadSession.completing_at < lease_cutoff,
            ),
        )
        # `expiring` is the sweep's OWN claim: no client claim accepts it, so
        # storage is safe to touch from here. It is stamped like any other
        # lease so a sweep that dies mid-reconciliation is retried rather than
        # stranding the session forever.
        .values(status="expiring", completing_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return now if result.rowcount == 1 else None


def finalize_expiry_sync(
    session: Session, *, session_id: uuid.UUID, lease: datetime
) -> bool:
    """Flip ``expiring`` → ``expired``, but ONLY if we still hold ``lease``.

    The sweep's storage calls (a HEAD plus an abort, each with the store's own
    timeouts and retries) can outlast its claim, at which point another sweep
    legitimately reclaims. Without the fence the first sweep would write the
    terminal state — and delete the recording — under the new owner (review
    round 13). Same contract as :func:`finalize_completion`.
    """
    result = session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.status == "expiring",
            MeetingUploadSession.completing_at == lease,
        )
        .values(status="expired")
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def claim_for_cancel(
    session: AsyncSession, *, session_id: uuid.UUID, stale_before: datetime
) -> datetime | None:
    """Atomically LEASE a session for CANCELLATION.

    Cancel used to take an ordinary ``completing`` lease, which reintroduced the
    very race the fence exists for (issue #37 review round 13): the store's
    configured timeouts and retries let ``abort_multipart`` outlive the lease, a
    completion then legitimately reclaims the row and finishes the upload, and
    the cancel — still believing it owns the session — wrote ``aborted`` and
    deleted the new owner's recording.

    ``cancelling`` is therefore its OWN state, and no completion claim accepts
    it: once cancellation owns the session the upload can no longer be
    completed, only cancelled or (if this cancel dies) reaped. A stale
    ``cancelling`` is reclaimable so a cancel whose abort never returned does
    not strand the row.

    Returns the lease token, which fences :func:`finalize_cancel`.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            or_(
                MeetingUploadSession.status == "active",
                and_(
                    MeetingUploadSession.status.in_(("completing", "cancelling")),
                    or_(
                        MeetingUploadSession.completing_at.is_(None),
                        MeetingUploadSession.completing_at < stale_before,
                    ),
                ),
            ),
        )
        .values(status="cancelling", completing_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return now if result.rowcount == 1 else None


async def finalize_cancel(
    session: AsyncSession, *, session_id: uuid.UUID, lease: datetime
) -> bool:
    """Flip ``cancelling`` → ``aborted``, but ONLY if we still hold ``lease``.

    ``abort_multipart`` can outlast the lease, so by the time it returns the
    expiry sweep may have reclaimed this row. Whoever still holds the lease
    writes the terminal state; the other abandons rather than deleting a
    recording it no longer owns (issue #37 review round 13).
    """
    result = await session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.status == "cancelling",
            MeetingUploadSession.completing_at == lease,
        )
        .values(status="aborted")
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def release_claim(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    lease: datetime,
    status: str,
) -> bool:
    """Hand a claimed session back, UNLEASED — only if we still hold ``lease``.

    A claim taken for work that turns out to be impossible (an indeterminate
    store probe during cancellation, say) must not be held until it expires:
    every retry in that window would be told the session is busy. Releasing it
    with a NULL lease makes it immediately claimable again, and the fence stops
    a holder whose claim was already reclaimed from resurrecting a dead state
    (issue #37 review round 14).
    """
    result = await session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.completing_at == lease,
        )
        .values(status=status, completing_at=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


def _aware(value: datetime) -> datetime:
    """SQLite round-trips DateTime(timezone=True) as naive UTC — normalize."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def fence_key_for_deletion_sync(
    session: Session, *, key: str, stale_before: datetime
) -> tuple[int, bool]:
    """Close the tap for ``key``, and report whether writers have DRAINED.

    Aborting a multipart upload is not the end of the story: an ``UploadPart``
    already running can land afterwards and leave its part stored (AWS documents
    this), so erasure cannot prove the multipart namespace is empty while a
    write it already admitted is still in flight (issue #37 review r17b).

    Two steps, and both matter:

    * **Fence.** Any session that is not already leased moves to ``expiring``,
      the state :func:`begin_part_write` refuses. Nothing NEW can start.
    * **Drain.** Report whether every live session for the key has zero
      in-flight writes and no fresh completion/cancellation lease. A counter
      left behind by an API process that died mid-request ages out via
      ``inflight_since``, so erasure is never blocked forever.

    Returns ``(sessions_fenced, drained)``. The caller waits on ``drained`` and
    refuses to erase while it is False — the deletion sweep retries.
    """
    now = datetime.now(UTC)
    result = session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.key == key,
            MeetingUploadSession.status.in_(LIVE_UPLOAD_SESSION_STATUSES),
            or_(
                MeetingUploadSession.status == "active",
                MeetingUploadSession.completing_at.is_(None),
                MeetingUploadSession.completing_at < stale_before,
            ),
        )
        .values(status="expiring", completing_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()

    live = (
        session.execute(
            select(MeetingUploadSession)
            .where(
                MeetingUploadSession.key == key,
                MeetingUploadSession.status.in_(LIVE_UPLOAD_SESSION_STATUSES),
            )
            # The counter and the leases are written by bulk UPDATEs with
            # `synchronize_session=False`, so a caller whose session already
            # holds these rows would otherwise read its own STALE copies and
            # conclude that writers had drained when they had not.
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    drained = True
    for row in live:
        leased = row.status in ("completing", "cancelling") and (
            row.completing_at is not None and _aware(row.completing_at) >= stale_before
        )
        if _writer_active(row, stale_before=stale_before) or leased:
            drained = False
    return result.rowcount, drained


async def begin_part_write(session: AsyncSession, *, session_id: uuid.UUID) -> bool:
    """ADMIT one part write, atomically with the "is this session live?" check.

    Checking the status and then uploading is a read-then-write: the deletion
    fence can land in between, and the part then completes after every
    abort/verify round, leaving audio the store reports as erased (issue #37
    review r17b). One conditional UPDATE closes that: after the fence flips the
    status this returns False, and before it the counter is already visible to
    the fence's drain check.
    """
    result = await session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.status == "active",
        )
        .values(
            inflight_writes=MeetingUploadSession.inflight_writes + 1,
            inflight_since=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def end_part_write(session: AsyncSession, *, session_id: uuid.UUID) -> None:
    """Release one admitted write. Never lets the counter go negative."""
    await session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.inflight_writes > 0,
        )
        .values(inflight_writes=MeetingUploadSession.inflight_writes - 1)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


def _writer_active(row: MeetingUploadSession, *, stale_before: datetime) -> bool:
    """Whether ``row`` has an admitted part write that must still be honoured.

    The counter is trusted while ``inflight_since`` is fresh. Freshness is not
    a guess about how long a write CAN take — the route heartbeats the
    timestamp for as long as the write actually runs — so a stale one means
    the process holding the admission is dead (issue #37 review round 18).
    """
    return row.inflight_writes > 0 and (
        row.inflight_since is None or _aware(row.inflight_since) >= stale_before
    )


async def session_writers_drained(
    session: AsyncSession, *, session_id: uuid.UUID, stale_before: datetime
) -> bool:
    """True once ``session_id`` has no admitted part write still in flight.

    ``populate_existing`` matters: the counter and heartbeat are written by
    bulk UPDATEs with ``synchronize_session=False`` (and by OTHER sessions),
    so a cached row would report its stale copy and the caller would conclude
    writers had drained while one was mid-stream.
    """
    row = (
        await session.execute(
            select(MeetingUploadSession)
            .where(MeetingUploadSession.id == session_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return True  # the row is gone — there is nothing left to wait for
    return not _writer_active(row, stale_before=stale_before)


def session_writers_drained_sync(
    session: Session, *, session_id: uuid.UUID, stale_before: datetime
) -> bool:
    """Sync twin of :func:`session_writers_drained` for the worker sweeps."""
    row = (
        session.execute(
            select(MeetingUploadSession)
            .where(MeetingUploadSession.id == session_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return True
    return not _writer_active(row, stale_before=stale_before)


async def touch_inflight(session: AsyncSession, *, session_id: uuid.UUID) -> None:
    """HEARTBEAT an admitted write: refresh ``inflight_since`` while it runs.

    This is what makes the freshness cutoff sound (issue #37 review round 18):
    without it, a genuinely live writer — a slow client streaming a large part —
    would eventually look leaked, and a later erasure retry would proceed
    around it. With it, only a write whose PROCESS died stops refreshing, and
    that one really is safe to age out.
    """
    await session.execute(
        update(MeetingUploadSession)
        .where(
            MeetingUploadSession.id == session_id,
            MeetingUploadSession.inflight_writes > 0,
        )
        .values(inflight_since=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
