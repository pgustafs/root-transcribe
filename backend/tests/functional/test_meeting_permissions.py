"""Functional tests for audio capability RBAC + the audit trail (#6, E1)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.meeting_governance import MeetingAuditEvent
from app.models.user import Role, User
from app.models.workspace import WorkspaceMember
from tests.identity import create_access_token

from .test_meetings_api import (
    _FakeAudioStore,
    _make_workspace,
    _seed_completed_recording,
    meetings_enabled,
)

pytestmark = pytest.mark.functional

API = "/api/v1"

__all__ = ["meetings_enabled"]  # re-exported fixture


async def _member_headers(
    db_session: AsyncSession, wid: str, role: str, email: str
) -> dict[str, str]:
    """Create a user and add them to the workspace with ``role``."""

    user = User(
        email=email,
        hashed_password="",
        role=Role.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    user.oidc_subject = str(user.id)
    user.oidc_issuer = settings.KEYCLOAK_ISSUER
    db_session.add(
        WorkspaceMember(workspace_id=uuid.UUID(wid), user_id=user.id, role=role)
    )
    await db_session.commit()
    return {"Authorization": f"Bearer {create_access_token(user)}"}


async def test_capability_matrix_and_grant_overrides(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio"
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    viewer = await _member_headers(db_session, wid, "viewer", "viewer@example.com")
    editor = await _member_headers(db_session, wid, "editor", "editor@example.com")

    # The server computes what the UI may show.
    caps = await client.get(
        f"{API}/workspaces/{wid}/meetings/permissions", headers=viewer
    )
    assert caps.status_code == 200
    assert caps.json()["capabilities"] == ["listen", "view_transcript"]

    # A viewer may listen but not download; an editor may edit but not approve.
    minted = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/session",
        headers=viewer,
    )
    assert minted.status_code == 200
    denied_dl = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/download",
        headers=viewer,
    )
    assert denied_dl.status_code == 403
    denied_export = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}"
        f"/revisions/{rec.latest_revision_id}/export",
        params={"format": "txt"},
        headers=editor,
    )
    assert denied_export.status_code == 403
    denied_approve = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}"
        f"/revisions/{rec.latest_revision_id}/approve",
        headers=editor,
        json={},
    )
    assert denied_approve.status_code == 403

    # A role-scoped grant opens the capability for every editor (audited).
    granted = await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=user_token_headers,  # workspace owner
        json={"capability": "export", "allow": True, "role": "editor"},
    )
    assert granted.status_code == 200, granted.text
    allowed_export = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}"
        f"/revisions/{rec.latest_revision_id}/export",
        params={"format": "txt"},
        headers=editor,
    )
    assert allowed_export.status_code == 200

    # A deny grant removes a default capability from the owner's own set.
    await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=user_token_headers,
        json={"capability": "download", "allow": False, "role": "owner"},
    )
    owner_denied = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/download",
        headers=user_token_headers,
    )
    assert owner_denied.status_code == 403

    # A grant must scope to exactly one of role/user.
    bad = await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=user_token_headers,
        json={"capability": "export", "allow": True},
    )
    assert bad.status_code == 400
    # Viewers cannot manage grants at all.
    forbidden = await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=viewer,
        json={"capability": "export", "allow": True, "role": "viewer"},
    )
    assert forbidden.status_code == 403


async def test_sensitive_operations_are_audited(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio"
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/session",
        headers=user_token_headers,
    )
    await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/download",
        headers=user_token_headers,
    )
    await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}"
        f"/revisions/{rec.latest_revision_id}/export",
        params={"format": "srt"},
        headers=user_token_headers,
    )
    from app.models.meeting_transcript_revision import MeetingTranscriptRevision

    revision2 = MeetingTranscriptRevision(
        recording_id=rec.id, revision_number=2, state="in_review"
    )
    db_session.add(revision2)
    await db_session.commit()
    await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/revisions/{revision2.id}/approve",
        headers=user_token_headers,
        json={"note": "ok"},
    )
    await client.delete(
        f"{API}/workspaces/{wid}/recordings/{rec.id}",
        headers=user_token_headers,
    )

    events = (await db_session.execute(select(MeetingAuditEvent))).scalars().all()
    actions = sorted(e.action for e in events)
    assert actions == [
        "audio.download",
        "audio.listen",
        "recording.delete",
        "revision.approve",
        "transcript.export",
    ]
    # Evidence survives its subjects: plain ids + denormalized actor email.
    export_event = next(e for e in events if e.action == "transcript.export")
    assert export_event.actor_email == normal_user.email
    assert export_event.target["format"] == "srt"
    assert export_event.recording_id == rec.id


async def test_restricted_sensitivity_gates_exfiltration_and_model_use(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Restricted recordings require explicit user grants for download/export.

    A request to an unregistered endpoint remains unavailable regardless of role.
    """
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio"
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    # The owner labels the recording restricted (approver-gated, audited).
    labeled = await client.patch(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/sensitivity",
        headers=user_token_headers,
        json={"sensitivity": "restricted"},
    )
    assert labeled.status_code == 200, labeled.text
    assert labeled.json()["sensitivity"] == "restricted"

    # Even the OWNER (default download/export) is refused now…
    denied_dl = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/download",
        headers=user_token_headers,
    )
    assert denied_dl.status_code == 403
    assert "restricted" in denied_dl.json()["detail"]
    denied_export = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}"
        f"/revisions/{rec.latest_revision_id}/export",
        params={"format": "txt"},
        headers=user_token_headers,
    )
    assert denied_export.status_code == 403
    # An unregistered endpoint stays unavailable regardless of capability grants.
    denied_source = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/save-as-source",
        headers=user_token_headers,
        json={},
    )
    assert denied_source.status_code == 404

    # An explicit USER-scoped grant unlocks download for that person only.
    await client.put(
        f"{API}/workspaces/{wid}/meetings/permissions/grants",
        headers=user_token_headers,
        json={
            "capability": "download",
            "allow": True,
            "user_id": str(normal_user.id),
        },
    )
    allowed = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio/download",
        headers=user_token_headers,
    )
    assert allowed.status_code == 200

    # The label change landed in the audit trail.
    events = (await db_session.execute(select(MeetingAuditEvent))).scalars().all()
    change = next(e for e in events if e.action == "sensitivity.change")
    assert change.target == {"from": "internal", "to": "restricted"}


async def test_legal_hold_blocks_delete_and_retention(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """A held recording cannot be deleted until the hold is released (E3);
    the retention-sweep skip is covered by the sync unit tests."""
    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio"
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    # Admin places the hold (legal_hold capability is ADMIN-only by default).
    held = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/hold",
        headers=admin_token_headers,
        json={"reason": "Litigation LIT-2026-042"},
    )
    assert held.status_code == 201, held.text
    # A non-admin owner has no legal_hold capability.
    denied = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/hold",
        headers=user_token_headers,
        json={"reason": "nope"},
    )
    assert denied.status_code == 403

    # User deletion is refused with the explicit hold error.
    blocked = await client.delete(
        f"{API}/workspaces/{wid}/recordings/{rec.id}",
        headers=user_token_headers,
    )
    assert blocked.status_code == 409
    assert "legal_hold_active" in blocked.json()["detail"]

    # The detail payload surfaces the hold for the UI badge.
    detail = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}", headers=user_token_headers
    )
    assert detail.json()["legal_hold"] is True

    # Release → deletion works again.
    released = await client.delete(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/hold",
        headers=admin_token_headers,
    )
    assert released.status_code == 204
    deleted = await client.delete(
        f"{API}/workspaces/{wid}/recordings/{rec.id}",
        headers=user_token_headers,
    )
    assert deleted.status_code == 204


async def test_workspace_retention_policy_api(
    client: AsyncClient,
    admin_token_headers: dict[str, str],
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    policy = await client.put(
        f"{API}/workspaces/{wid}/meetings/retention",
        headers=admin_token_headers,
        json={"artifact": "raw_audio", "retention_days": 7},
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["retention_days"] == 7
    listed = await client.get(
        f"{API}/workspaces/{wid}/meetings/retention",
        headers=admin_token_headers,
    )
    assert [p["artifact"] for p in listed.json()] == ["raw_audio"]
    # Unknown artifacts and non-admins are refused.
    bad = await client.put(
        f"{API}/workspaces/{wid}/meetings/retention",
        headers=admin_token_headers,
        json={"artifact": "everything", "retention_days": 1},
    )
    assert bad.status_code == 400
    denied = await client.put(
        f"{API}/workspaces/{wid}/meetings/retention",
        headers=user_token_headers,
        json={"artifact": "raw_audio", "retention_days": 7},
    )
    assert denied.status_code == 403


async def test_denied_capabilities_gate_transcript_and_audio(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """Deny-grants must bite on the CONTENT paths (review finding): a member
    denied view_transcript polls status but gets no segments, and a member
    denied listen cannot fetch the raw audio with a bearer token."""
    from app.models.meeting_governance import MeetingPermissionGrant

    wid = await _make_workspace(client, user_token_headers)
    meetings_enabled.blobs["k1"] = b"audio-bytes"
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    viewer_headers = await _member_headers(
        db_session, wid, "viewer", "denied-viewer@example.com"
    )
    viewer = (
        (
            await db_session.execute(
                select(User).where(User.email == "denied-viewer@example.com")
            )
        )
        .scalars()
        .one()
    )
    db_session.add_all(
        [
            MeetingPermissionGrant(
                workspace_id=uuid.UUID(wid),
                user_id=viewer.id,
                capability="view_transcript",
                allow=False,
            ),
            MeetingPermissionGrant(
                workspace_id=uuid.UUID(wid),
                user_id=viewer.id,
                capability="listen",
                allow=False,
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}", headers=viewer_headers
    )
    assert resp.status_code == 200
    assert resp.json()["segments"] == []  # status yes, transcript no

    resp = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/audio", headers=viewer_headers
    )
    assert resp.status_code == 403

    # The un-denied owner still gets both.
    resp = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}", headers=user_token_headers
    )
    assert len(resp.json()["segments"]) > 0


async def test_denied_view_transcript_cannot_read_or_decide_revisions(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    """A member denied ``view_transcript`` may not read revision metadata or
    approve/discard a transcript (issue #39 review round 1).

    The two capabilities are grantable independently, so an editor keeping
    ``approve_transcript`` while ``view_transcript`` is explicitly denied could
    sign off on a transcript they are not allowed to read — and the revision
    list carries the free-text approval note, so the denial leaked content as
    well as authority.
    """
    from app.models.meeting_governance import MeetingPermissionGrant

    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)

    manager_headers = await _member_headers(
        db_session, wid, "manager", "blind-approver@example.com"
    )
    manager = (
        (
            await db_session.execute(
                select(User).where(User.email == "blind-approver@example.com")
            )
        )
        .scalars()
        .one()
    )
    db_session.add(
        MeetingPermissionGrant(
            workspace_id=uuid.UUID(wid),
            user_id=manager.id,
            capability="view_transcript",
            allow=False,
        )
    )
    await db_session.commit()

    listed = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/revisions",
        headers=manager_headers,
    )
    assert listed.status_code == 403, listed.text

    approved = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/revisions/"
        f"{rec.latest_revision_id}/approve",
        headers=manager_headers,
        json={},
    )
    assert approved.status_code == 403, approved.text

    discarded = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/revisions/"
        f"{rec.latest_revision_id}/discard",
        headers=manager_headers,
    )
    assert discarded.status_code == 403, discarded.text

    # ...and the capability alone is what gates it: with view restored, the
    # same caller can read the list again.
    grant = (
        (
            await db_session.execute(
                select(MeetingPermissionGrant).where(
                    MeetingPermissionGrant.user_id == manager.id
                )
            )
        )
        .scalars()
        .one()
    )
    grant.allow = True
    await db_session.commit()
    listed_again = await client.get(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/revisions",
        headers=manager_headers,
    )
    assert listed_again.status_code == 200, listed_again.text
