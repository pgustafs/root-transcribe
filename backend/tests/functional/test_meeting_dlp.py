"""Functional tests for DLP terms, findings & redaction API (#6, wave E4)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meeting_dlp import DlpFinding
from app.models.meeting_governance import MeetingPermissionGrant
from app.models.task_outbox import TaskOutbox
from app.models.user import User

from .test_meetings_api import (
    _FakeAudioStore,
    _make_workspace,
    _seed_completed_recording,
    meetings_enabled,
)

pytestmark = pytest.mark.functional

API = "/api/v1"

__all__ = ["meetings_enabled"]  # re-exported fixture


async def _grant_redact(db_session: AsyncSession, wid: str, user: User) -> None:
    """REDACT is ADMIN-only by default — grant it to the workspace user."""
    db_session.add(
        MeetingPermissionGrant(
            workspace_id=uuid.UUID(wid),
            user_id=user.id,
            capability="redact",
            allow=True,
        )
    )
    await db_session.commit()


async def test_dlp_terms_crud_validates_regex(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    await _grant_redact(db_session, wid, normal_user)
    base = f"{API}/workspaces/{wid}/meetings/dlp-terms"

    resp = await client.post(
        base,
        json={"pattern": "[broken", "is_regex": True},
        headers=user_token_headers,
    )
    assert resp.status_code == 422

    resp = await client.post(
        base,
        json={"pattern": "Project Falcon", "label": "codename"},
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    term_id = resp.json()["id"]

    resp = await client.get(base, headers=user_token_headers)
    assert [t["pattern"] for t in resp.json()] == ["Project Falcon"]

    resp = await client.delete(f"{base}/{term_id}", headers=user_token_headers)
    assert resp.status_code == 204
    resp = await client.get(base, headers=user_token_headers)
    assert resp.json() == []


async def test_dlp_review_and_redaction_flow(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    await _grant_redact(db_session, wid, normal_user)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}"

    # Seed a pending finding as the scan task would have left it.
    from app.models.meeting_transcript_segment import MeetingTranscriptSegment

    seg = (
        (
            await db_session.execute(
                select(MeetingTranscriptSegment)
                .where(MeetingTranscriptSegment.recording_id == rec.id)
                .order_by(MeetingTranscriptSegment.idx)
            )
        )
        .scalars()
        .first()
    )
    assert seg is not None
    finding = DlpFinding(
        workspace_id=uuid.UUID(wid),
        recording_id=rec.id,
        revision_id=seg.revision_id,
        segment_id=seg.id,
        detector="email",
        matched_text="a@b.se",
        start_char=0,
        end_char=6,
        start_ms=seg.start_ms,
        end_ms=seg.end_ms,
        status="pending",
    )
    db_session.add(finding)
    await db_session.commit()

    # Redaction refuses while nothing is accepted.
    resp = await client.post(f"{base}/redact", headers=user_token_headers)
    assert resp.status_code == 409

    # Review: list → accept.
    resp = await client.get(f"{base}/dlp", headers=user_token_headers)
    assert resp.status_code == 200
    assert [f["detector"] for f in resp.json()] == ["email"]
    resp = await client.post(
        f"{base}/dlp/findings/{finding.id}/status",
        json={"status": "accepted"},
        headers=user_token_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    # Manual finding on a chosen span arrives pre-accepted (bounds clamped).
    resp = await client.post(
        f"{base}/dlp/findings",
        json={"segment_id": str(seg.id), "start_char": 0, "end_char": 999},
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["detector"] == "manual"
    assert resp.json()["status"] == "accepted"
    assert resp.json()["end_char"] == len(seg.text)

    # Apply: durable enqueue on the transcription queue + audit.
    resp = await client.post(f"{base}/redact", headers=user_token_headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["findings"] == "2"
    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.task_name == "tasks.apply_meeting_redaction"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(outbox) == 1
    assert outbox[0].queue == "transcription"

    # On-demand rescan is a durable enqueue too.
    resp = await client.post(f"{base}/dlp/scan", headers=user_token_headers)
    assert resp.status_code == 202
    scans = (
        (
            await db_session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.task_name == "tasks.scan_meeting_dlp"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(scans) == 1
