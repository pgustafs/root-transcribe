"""Speaker-directory repository (profiles + voiceprints).

Async functions back the API (directory management + poll); sync helpers drive the
Celery enrollment task and the transcription matcher (which reads active
voiceprints to build per-profile centroids). This layer never encrypts/decrypts —
that is :mod:`app.services.speaker_directory` — it only moves opaque blobs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.speaker_profile import SpeakerProfile, SpeakerVoiceprint
from app.models.user import User


def lock_for_update_sync(
    session: Session, profile_id: uuid.UUID
) -> SpeakerProfile | None:
    """Take a ``FOR UPDATE`` lock on the profile row and RETURN it (worker side).

    The serialization boundary for the consent-lifecycle change AND the raw
    biometric read: a withdrawal takes the same lock, so a read either happens
    entirely before it (and is skipped once the withdrawn state is seen) or
    after the read already began under live consent. No raw read starts after a
    committed withdrawal (issue #14 review). The returned row lets callers also
    check ``status`` (a deactivated profile is not eligible for enrollment) under the
    same lock. A no-op on SQLite.
    """
    return (
        session.execute(
            select(SpeakerProfile)
            .where(SpeakerProfile.id == profile_id)
            .with_for_update()
        )
        .scalars()
        .first()
    )


# --- async (API) ---


async def create(
    session: AsyncSession,
    *,
    display_name: str,
    user_id: uuid.UUID | None,
    consent_granted: bool,
    actor: User,
) -> SpeakerProfile:
    """Create a directory entry; stamp consent time if consent was given.

    Retention is mandatory (issue #6): unless disabled by policy, every profile
    gets a default ``retention_delete_at`` so biometric data cannot linger
    indefinitely by omission.
    """
    from app.core.config import settings

    retention_at = None
    if settings.VOICEPRINT_RETENTION_DAYS_DEFAULT > 0:
        retention_at = datetime.now(UTC) + timedelta(
            days=settings.VOICEPRINT_RETENTION_DAYS_DEFAULT
        )
    profile = SpeakerProfile(
        display_name=display_name,
        user_id=user_id,
        status="active",
        consent_granted_at=datetime.now(UTC) if consent_granted else None,
        retention_delete_at=retention_at,
        created_by_id=actor.id,
        created_by_email=actor.email,
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def get(session: AsyncSession, profile_id: uuid.UUID) -> SpeakerProfile | None:
    """Fetch one profile."""
    return await session.get(SpeakerProfile, profile_id)


async def get_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> SpeakerProfile | None:
    """The profile linked to an application user (self-enrollment), if any."""
    result = await session.execute(
        select(SpeakerProfile).where(SpeakerProfile.user_id == user_id)
    )
    return result.scalars().first()


async def list_all(
    session: AsyncSession, *, limit: int = 500
) -> list[tuple[SpeakerProfile, int]]:
    """All profiles with their voiceprint counts, newest first."""
    count_col = func.count(SpeakerVoiceprint.id)
    result = await session.execute(
        select(SpeakerProfile, count_col)
        .outerjoin(
            SpeakerVoiceprint,
            SpeakerVoiceprint.speaker_profile_id == SpeakerProfile.id,
        )
        .group_by(SpeakerProfile.id)
        .order_by(SpeakerProfile.created_at.desc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result.all()]


async def count_voiceprints(session: AsyncSession, profile_id: uuid.UUID) -> int:
    """How many voiceprints a profile has enrolled."""
    result = await session.execute(
        select(func.count(SpeakerVoiceprint.id)).where(
            SpeakerVoiceprint.speaker_profile_id == profile_id
        )
    )
    return int(result.scalar_one())


async def update(
    session: AsyncSession,
    profile: SpeakerProfile,
    *,
    display_name: str | None = None,
    status: str | None = None,
) -> SpeakerProfile:
    """Rename or (de)activate a profile."""
    if display_name is not None:
        profile.display_name = display_name
    if status is not None:
        profile.status = status
    profile.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(profile)
    return profile


async def delete_profile(session: AsyncSession, profile: SpeakerProfile) -> None:
    """Delete a profile and all its voiceprints (cascade)."""
    await session.delete(profile)
    await session.commit()


async def delete_voiceprint(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    voiceprint_id: uuid.UUID,
    commit: bool = True,
) -> bool:
    """Delete one voiceprint; returns False if it wasn't found for that profile.

    ``commit=False`` lets an erasure loop delete several voiceprints AND
    tombstone pending samples in ONE transaction while holding the profile lock,
    so a queued enrollment worker cannot slip a new voiceprint in between
    per-item commits (issue #14 review)."""
    result = await session.execute(
        delete(SpeakerVoiceprint).where(
            SpeakerVoiceprint.id == voiceprint_id,
            SpeakerVoiceprint.speaker_profile_id == profile_id,
        )
    )
    if commit:
        await session.commit()
    return bool(result.rowcount)


async def list_voiceprints(
    session: AsyncSession, profile_id: uuid.UUID
) -> Sequence[SpeakerVoiceprint]:
    """A profile's voiceprints (metadata; the API never returns the embedding)."""
    result = await session.execute(
        select(SpeakerVoiceprint)
        .where(SpeakerVoiceprint.speaker_profile_id == profile_id)
        .order_by(SpeakerVoiceprint.created_at.desc())
    )
    return result.scalars().all()


# --- sync (Celery worker) ---


def get_sync(session: Session, profile_id: uuid.UUID) -> SpeakerProfile | None:
    """Fetch one profile (worker)."""
    return session.get(SpeakerProfile, profile_id)


def add_voiceprint_sync(
    session: Session,
    *,
    profile_id: uuid.UUID,
    embedding_blob: bytes,
    dims: int,
    model_id: str,
    sample_key: str | None,
    key_id: str | None = None,
    model_revision: str | None = None,
    commit: bool = True,
) -> SpeakerVoiceprint:
    """Persist an enrolled (already-encrypted) voiceprint (records which
    keyring key encrypted it and the embedding contract, issue #6).

    ``commit=False`` lets the enrollment worker commit the voiceprint in the
    SAME transaction as its idempotency claim (review round 3).
    """
    from app.services.speaker_directory import active_key_id

    vp = SpeakerVoiceprint(
        speaker_profile_id=profile_id,
        embedding=embedding_blob,
        dims=dims,
        model_id=model_id,
        model_revision=model_revision,
        sample_key=sample_key,
        key_id=key_id or active_key_id(),
    )
    session.add(vp)
    if commit:
        session.commit()
        session.refresh(vp)
    else:
        session.flush()
    return vp


def list_active_voiceprints_sync(
    session: Session,
) -> list[SpeakerVoiceprint]:
    """All voiceprints for **active, consented** profiles (matching candidates)."""
    result = session.execute(
        select(SpeakerVoiceprint)
        .join(
            SpeakerProfile,
            SpeakerProfile.id == SpeakerVoiceprint.speaker_profile_id,
        )
        .where(
            SpeakerProfile.status == "active",
            SpeakerProfile.consent_granted_at.is_not(None),
        )
    )
    return list(result.scalars().all())
