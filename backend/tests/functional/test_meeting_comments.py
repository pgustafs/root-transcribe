"""Functional tests for transcript comments & mentions (#6, wave D4)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
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


async def test_comment_thread_lifecycle_with_anchor(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}/comments"

    # Root comment anchored to a moment of the recording.
    resp = await client.post(
        base,
        json={
            "body": "Was this the final number?",
            "at_ms": 65_000,
            "anchor_quote": "Hello everyone.",
        },
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    root = resp.json()
    assert root["at_ms"] == 65_000
    assert root["anchor_quote"] == "Hello everyone."

    # Reply attaches to the root; replying to a reply is refused.
    resp = await client.post(
        base,
        json={"body": "Yes, confirmed.", "parent_id": root["id"]},
        headers=user_token_headers,
    )
    assert resp.status_code == 200
    reply = resp.json()
    resp = await client.post(
        base,
        json={"body": "nested?", "parent_id": reply["id"]},
        headers=user_token_headers,
    )
    assert resp.status_code == 409

    # Resolve hides the thread from the default listing; reopen restores it.
    resp = await client.post(f"{base}/{root['id']}/resolve", headers=user_token_headers)
    assert resp.status_code == 200
    assert resp.json()["resolved_at"] is not None
    resp = await client.get(base, headers=user_token_headers)
    assert resp.json() == []
    resp = await client.get(
        base, params={"include_resolved": "true"}, headers=user_token_headers
    )
    assert len(resp.json()) == 2
    resp = await client.post(f"{base}/{root['id']}/reopen", headers=user_token_headers)
    assert resp.status_code == 200
    resp = await client.get(base, headers=user_token_headers)
    assert len(resp.json()) == 2

    # Author edit; root delete removes the reply too.
    resp = await client.patch(
        f"{base}/{root['id']}",
        json={"body": "Was this the FINAL number?"},
        headers=user_token_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["body"] == "Was this the FINAL number?"
    resp = await client.delete(f"{base}/{root['id']}", headers=user_token_headers)
    assert resp.status_code == 204
    resp = await client.get(base, headers=user_token_headers)
    assert resp.json() == []


async def test_comment_mentions_notify_members_only(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
) -> None:
    from app.models.user import Role
    from app.models.workspace import WorkspaceMember

    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}/comments"

    member = User(
        email="colleague@example.com",
        hashed_password="",
        role=Role.USER,
        is_active=True,
    )
    outsider = User(
        email="outsider@example.com",
        hashed_password="",
        role=Role.USER,
        is_active=True,
    )
    db_session.add_all([member, outsider])
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=uuid.UUID(wid), user_id=member.id, role="editor")
    )
    await db_session.commit()

    # Mentioning a non-member is refused outright.
    resp = await client.post(
        base,
        json={"body": "cc @outsider", "mentions": [str(outsider.id)]},
        headers=user_token_headers,
    )
    assert resp.status_code == 409

    # Mentioning a member lands a notification deep-linking to the moment.
    resp = await client.post(
        base,
        json={
            "body": "look at this @colleague",
            "at_ms": 30_000,
            "mentions": [str(member.id)],
        },
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    notes = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == member.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(notes) == 1
    assert notes[0].type == "mention"
    assert notes[0].link == f"/workspaces/{wid}/meetings?recording={rec.id}&t=30"
