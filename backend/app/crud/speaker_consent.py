"""Consent-ledger + speaker-suggestion repository (issue #6).

Consent rows are immutable; the current state of a (profile, purpose) pair is
its latest row. The sync helpers back the worker's candidate filtering; the
async helpers back the consent and review-queue APIs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.speaker_consent import SpeakerConsentRecord, SpeakerSuggestion
from app.models.user import User

# --- consent ledger ---


async def record(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    purpose: str,
    status: str,
    policy_version: str,
    actor: User,
    on_behalf: bool = False,
    evidence: dict | None = None,
    workspace_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    withdrawal_reason: str | None = None,
) -> SpeakerConsentRecord:
    """Append one immutable consent act (flushed; caller commits)."""
    row = SpeakerConsentRecord(
        profile_id=profile_id,
        purpose=purpose,
        status=status,
        policy_version=policy_version,
        actor_id=actor.id,
        actor_email=actor.email,
        on_behalf=on_behalf,
        evidence=evidence,
        workspace_id=workspace_id,
        expires_at=expires_at,
        withdrawn_at=datetime.now(UTC) if status == "withdrawn" else None,
        withdrawal_reason=withdrawal_reason,
    )
    session.add(row)
    await session.flush()
    return row


async def list_for_profile(
    session: AsyncSession, profile_id: uuid.UUID
) -> Sequence[SpeakerConsentRecord]:
    result = await session.execute(
        select(SpeakerConsentRecord)
        .where(SpeakerConsentRecord.profile_id == profile_id)
        .order_by(SpeakerConsentRecord.created_at.desc())
    )
    return result.scalars().all()


def effective_status(row: SpeakerConsentRecord) -> str:
    """A record's status with expiry applied (``granted`` past expiry ⇒
    ``expired``)."""
    if row.status == "granted" and row.expires_at is not None:
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return "expired"
    return row.status


def _applicable_record_stmt(
    profile_id: uuid.UUID, purpose: str, workspace_id: uuid.UUID | None
):
    """The latest APPLICABLE ledger row for (profile, purpose, scope).

    Rows with ``workspace_id`` NULL are global; for a given workspace the
    latest row among {global, that workspace} wins. Without a workspace only
    global rows count — a grant scoped to one workspace never authorizes
    another.
    """
    stmt = (
        select(SpeakerConsentRecord)
        .where(
            SpeakerConsentRecord.profile_id == profile_id,
            SpeakerConsentRecord.purpose == purpose,
        )
        .order_by(SpeakerConsentRecord.created_at.desc())
        .limit(1)
    )
    if workspace_id is not None:
        return stmt.where(
            (SpeakerConsentRecord.workspace_id.is_(None))
            | (SpeakerConsentRecord.workspace_id == workspace_id)
        )
    return stmt.where(SpeakerConsentRecord.workspace_id.is_(None))


async def current_record(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    purpose: str,
    workspace_id: uuid.UUID | None = None,
) -> SpeakerConsentRecord | None:
    """The latest applicable consent record (evidence for audit snapshots)."""
    result = await session.execute(
        _applicable_record_stmt(profile_id, purpose, workspace_id)
    )
    return result.scalar_one_or_none()


def current_record_sync(
    session: Session,
    *,
    profile_id: uuid.UUID,
    purpose: str,
    workspace_id: uuid.UUID | None = None,
) -> SpeakerConsentRecord | None:
    """Sync twin of :func:`current_record` (worker-side re-verification)."""
    return session.execute(
        _applicable_record_stmt(profile_id, purpose, workspace_id)
    ).scalar_one_or_none()


async def current_status(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    purpose: str,
    workspace_id: uuid.UUID | None = None,
) -> str | None:
    """The latest APPLICABLE consent status for a purpose (None = never)."""
    row = await current_record(
        session, profile_id=profile_id, purpose=purpose, workspace_id=workspace_id
    )
    return None if row is None else effective_status(row)


def consented_profile_ids_sync(
    session: Session, *, purpose: str, workspace_id: uuid.UUID | None = None
) -> set[uuid.UUID]:
    """Profiles whose LATEST applicable consent record for ``purpose`` is a
    live grant.

    Scope matters (review finding): a consent row with ``workspace_id`` NULL
    is a global grant; a row scoped to a workspace applies only there. For a
    given ``workspace_id`` the latest row among {global rows, rows scoped to
    THAT workspace} wins — a grant scoped to workspace A never enables
    matching in workspace B.

    The worker's matching-candidate filter: withdrawn or expired consent
    excludes a profile immediately, no row edits required.
    """
    stmt = (
        select(SpeakerConsentRecord)
        .where(SpeakerConsentRecord.purpose == purpose)
        .order_by(SpeakerConsentRecord.profile_id, SpeakerConsentRecord.created_at)
    )
    if workspace_id is not None:
        stmt = stmt.where(
            (SpeakerConsentRecord.workspace_id.is_(None))
            | (SpeakerConsentRecord.workspace_id == workspace_id)
        )
    else:
        stmt = stmt.where(SpeakerConsentRecord.workspace_id.is_(None))
    rows = session.execute(stmt).scalars().all()
    latest: dict[uuid.UUID, SpeakerConsentRecord] = {}
    for row in rows:  # ordered by created_at ⇒ the last seen per profile wins
        latest[row.profile_id] = row
    now = datetime.now(UTC)
    granted: set[uuid.UUID] = set()
    for profile_id, row in latest.items():
        if row.status != "granted":
            continue
        expires = row.expires_at
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= now:
                continue
        granted.add(profile_id)
    return granted


# --- suggestion review queue ---


def add_suggestion_sync(
    session: Session,
    *,
    recording_id: uuid.UUID,
    revision_id: uuid.UUID,
    speaker_label: str,
    suggested_profile_id: uuid.UUID,
    score: float,
    margin: float,
    sample_turns: int,
) -> SpeakerSuggestion:
    row = SpeakerSuggestion(
        recording_id=recording_id,
        revision_id=revision_id,
        speaker_label=speaker_label,
        suggested_profile_id=suggested_profile_id,
        score=score,
        margin=margin,
        sample_turns=sample_turns,
    )
    session.add(row)
    session.flush()
    return row


async def list_pending_for_recording(
    session: AsyncSession, *, recording_id: uuid.UUID, revision_id: uuid.UUID | None
) -> Sequence[SpeakerSuggestion]:
    stmt = (
        select(SpeakerSuggestion)
        .where(
            SpeakerSuggestion.recording_id == recording_id,
            SpeakerSuggestion.status == "pending",
        )
        .order_by(SpeakerSuggestion.created_at)
    )
    if revision_id is not None:
        stmt = stmt.where(SpeakerSuggestion.revision_id == revision_id)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_suggestion(
    session: AsyncSession, *, recording_id: uuid.UUID, suggestion_id: uuid.UUID
) -> SpeakerSuggestion | None:
    result = await session.execute(
        select(SpeakerSuggestion).where(
            SpeakerSuggestion.id == suggestion_id,
            SpeakerSuggestion.recording_id == recording_id,
        )
    )
    return result.scalar_one_or_none()


async def decide_suggestion(
    session: AsyncSession,
    suggestion: SpeakerSuggestion,
    *,
    status: str,
    actor: User,
) -> SpeakerSuggestion:
    """Record the human decision (accepted/rejected); flushed, caller commits."""
    suggestion.status = status
    suggestion.decided_by_id = actor.id
    suggestion.decided_by_email = actor.email
    suggestion.decided_at = datetime.now(UTC)
    await session.flush()
    return suggestion
