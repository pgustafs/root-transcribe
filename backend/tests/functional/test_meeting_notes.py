"""Functional tests for note agents and meeting notes."""

from __future__ import annotations

import io
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.meeting_note import MeetingNote, NoteAgent
from app.models.task_outbox import TaskOutbox
from app.models.user import User
from app.services.note_defaults import BUILTIN_AGENTS, BUILTIN_BY_KEY

from .test_meetings_api import (
    _FakeAudioStore,
    _make_workspace,
    _seed_completed_recording,
    meetings_enabled,
)

pytestmark = pytest.mark.functional

API = "/api/v1"

__all__ = ["meetings_enabled"]  # re-exported fixture


@pytest.fixture
async def builtins(db_session: AsyncSession) -> dict[str, NoteAgent]:
    """The agents the migration seeds (tests build the schema from metadata)."""
    agents = {
        agent.key: NoteAgent(
            builtin_key=agent.key,
            name=agent.name,
            description=agent.description,
            instructions=agent.instructions,
        )
        for agent in BUILTIN_AGENTS
    }
    db_session.add_all(agents.values())
    await db_session.commit()
    return agents


@pytest.fixture
def llm_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://llm.invalid/v1")


async def _ready_note(
    session: AsyncSession, rec, agent: NoteAgent, text: str = "## Summary\n\nHello."
) -> MeetingNote:
    note = MeetingNote(
        workspace_id=rec.workspace_id,
        recording_id=rec.id,
        revision_id=rec.approved_revision_id,
        agent_id=agent.id,
        agent_name=agent.name,
        instructions=agent.instructions,
        status="ready",
        generated_markdown=text,
        content_markdown=text,
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return note


async def test_config_reports_notes_status(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get(f"{API}/config")).json()["notes_status"] == "unavailable"
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://llm.invalid/v1")
    assert (await client.get(f"{API}/config")).json()["notes_status"] == "available"


async def test_agents_shared_and_personal(
    client: AsyncClient,
    builtins: dict[str, NoteAgent],
    user_token_headers: dict[str, str],
    power_user_token_headers: dict[str, str],
    admin_token_headers: dict[str, str],
) -> None:
    listed = (await client.get(f"{API}/note-agents", headers=user_token_headers)).json()
    assert [a["name"] for a in listed] == ["Summary", "Task list"]
    assert all(a["shared"] and a["builtin"] and not a["can_edit"] for a in listed)
    assert not any(a["modified"] for a in listed)
    summary_id = listed[0]["id"]

    # A normal user cannot change or add shared agents...
    resp = await client.patch(
        f"{API}/note-agents/{summary_id}",
        json={"instructions": "Be brief."},
        headers=user_token_headers,
    )
    assert resp.status_code == 403
    resp = await client.post(
        f"{API}/note-agents",
        json={"name": "Risks", "instructions": "List risks.", "shared": True},
        headers=user_token_headers,
    )
    assert resp.status_code == 403
    # ...but can keep personal ones, which nobody else sees.
    resp = await client.post(
        f"{API}/note-agents",
        json={"name": " My notes ", "instructions": "Write my notes."},
        headers=user_token_headers,
    )
    assert resp.status_code == 201, resp.text
    mine = resp.json()
    assert (mine["name"], mine["shared"], mine["can_edit"]) == ("My notes", False, True)
    others = (
        await client.get(f"{API}/note-agents", headers=power_user_token_headers)
    ).json()
    assert "My notes" not in [a["name"] for a in others]
    assert all(a["can_edit"] for a in others)
    resp = await client.patch(
        f"{API}/note-agents/{mine['id']}",
        json={"name": "Hijacked"},
        headers=admin_token_headers,
    )
    assert resp.status_code == 404  # not visible to others, admins included

    # A power user edits the shared built-in; reset brings the default back.
    resp = await client.patch(
        f"{API}/note-agents/{summary_id}",
        json={"instructions": "Be brief.", "is_active": False},
        headers=power_user_token_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["modified"] is True and resp.json()["is_active"] is False
    resp = await client.post(
        f"{API}/note-agents/{summary_id}/reset", headers=power_user_token_headers
    )
    assert resp.json()["instructions"] == BUILTIN_BY_KEY["summary"].instructions
    assert resp.json()["modified"] is False
    assert resp.json()["is_active"] is False  # reset restores text, not the switch

    # Built-ins cannot be deleted; own and added shared agents can.
    resp = await client.delete(
        f"{API}/note-agents/{summary_id}", headers=admin_token_headers
    )
    assert resp.status_code == 409
    resp = await client.post(
        f"{API}/note-agents/{mine['id']}/reset", headers=user_token_headers
    )
    assert resp.status_code == 409
    resp = await client.delete(
        f"{API}/note-agents/{mine['id']}", headers=user_token_headers
    )
    assert resp.status_code == 204


async def test_note_create_is_durable_and_gated(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    builtins: dict[str, NoteAgent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}/notes"
    payload = {"agent_id": str(builtins["summary"].id)}

    # No LLM configured: refuse instead of queueing work nobody can do.
    resp = await client.post(base, json=payload, headers=user_token_headers)
    assert resp.status_code == 503
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://llm.invalid/v1")

    resp = await client.post(base, json=payload, headers=user_token_headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["status"], body["agent_name"]) == ("pending", "Summary")
    assert body["revision_id"] == str(rec.approved_revision_id)
    assert body["content_markdown"] is None

    outbox = (
        (
            await db_session.execute(
                select(TaskOutbox).where(
                    TaskOutbox.task_name == "tasks.generate_meeting_note"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(outbox) == 1
    assert outbox[0].payload == {"note_id": body["id"]}
    assert outbox[0].queue == "celery"
    # The instructions are snapshotted: a later agent edit does not change them.
    note = await db_session.get(MeetingNote, uuid.UUID(body["id"]))
    assert note.instructions == BUILTIN_BY_KEY["summary"].instructions

    listed = (await client.get(base, headers=user_token_headers)).json()
    assert [n["id"] for n in listed] == [body["id"]]

    # Unknown, switched-off and other people's agents are refused.
    resp = await client.post(
        base, json={"agent_id": str(uuid.uuid4())}, headers=user_token_headers
    )
    assert resp.status_code == 404
    builtins["tasks"].is_active = False
    await db_session.commit()
    resp = await client.post(
        base, json={"agent_id": str(builtins["tasks"].id)}, headers=user_token_headers
    )
    assert resp.status_code == 409

    # Restricted recording: sending the transcript to the model needs an
    # explicit user grant, like an export.
    rec.sensitivity = "restricted"
    await db_session.commit()
    resp = await client.post(base, json=payload, headers=user_token_headers)
    assert resp.status_code == 403


async def test_note_requires_a_finished_transcript(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    builtins: dict[str, NoteAgent],
    llm_configured: None,
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    rec.status = "transcribing"
    await db_session.commit()
    resp = await client.post(
        f"{API}/workspaces/{wid}/recordings/{rec.id}/notes",
        json={"agent_id": str(builtins["summary"].id)},
        headers=user_token_headers,
    )
    assert resp.status_code == 409


async def test_note_edit_restore_export_delete(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    builtins: dict[str, NoteAgent],
) -> None:
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    note = await _ready_note(db_session, rec, builtins["tasks"])
    url = f"{API}/workspaces/{wid}/recordings/{rec.id}/notes/{note.id}"

    edited = "## Tasks\n\n- [ ] **Anna** — book the room\n"
    resp = await client.patch(
        url,
        json={"content_markdown": edited, "expected_version": 1},
        headers=user_token_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["version"], body["edited"]) == (2, True)
    assert body["edited_by_email"] == normal_user.email
    assert body["content_markdown"] == edited

    # A save based on the old version does not overwrite the newer one.
    resp = await client.patch(
        url,
        json={"content_markdown": "stale", "expected_version": 1},
        headers=user_token_headers,
    )
    assert resp.status_code == 409

    # Exports: Markdown as text, Word and PDF as real documents.
    resp = await client.get(f"{url}/export?format=md", headers=user_token_headers)
    assert resp.status_code == 200
    assert resp.text == edited
    assert 'filename="Seeded - Task list.md"' in resp.headers["content-disposition"]
    resp = await client.get(f"{url}/export?format=docx", headers=user_token_headers)
    assert resp.status_code == 200
    from docx import Document

    paragraphs = [p.text for p in Document(io.BytesIO(resp.content)).paragraphs]
    assert paragraphs == ["Tasks", "☐ Anna — book the room"]
    resp = await client.get(f"{url}/export?format=pdf", headers=user_token_headers)
    assert resp.status_code == 200 and resp.content.startswith(b"%PDF-")
    resp = await client.get(f"{url}/export?format=odt", headers=user_token_headers)
    assert resp.status_code == 400

    resp = await client.post(f"{url}/restore", headers=user_token_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["version"], body["edited"]) == (3, False)
    assert body["content_markdown"] == "## Summary\n\nHello."

    resp = await client.delete(url, headers=user_token_headers)
    assert resp.status_code == 204
    assert (
        await client.get(url + "/export", headers=user_token_headers)
    ).status_code == 404


async def test_notes_follow_workspace_permissions(
    client: AsyncClient,
    db_session: AsyncSession,
    normal_user: User,
    power_user: User,
    user_token_headers: dict[str, str],
    power_user_token_headers: dict[str, str],
    meetings_enabled: _FakeAudioStore,
    builtins: dict[str, NoteAgent],
    llm_configured: None,
) -> None:
    """A viewer reads notes but cannot create, edit or delete them; a
    non-member sees nothing."""
    wid = await _make_workspace(client, user_token_headers)
    rec = await _seed_completed_recording(db_session, uuid.UUID(wid), normal_user)
    note = await _ready_note(db_session, rec, builtins["summary"])
    base = f"{API}/workspaces/{wid}/recordings/{rec.id}/notes"

    resp = await client.get(base, headers=power_user_token_headers)
    assert resp.status_code in (403, 404)

    added = await client.post(
        f"{API}/workspaces/{wid}/members",
        json={"email": power_user.email, "role": "viewer"},
        headers=user_token_headers,
    )
    assert added.status_code == 201, added.text
    resp = await client.get(base, headers=power_user_token_headers)
    assert resp.status_code == 200 and len(resp.json()) == 1
    for method, url, kwargs in [
        ("post", base, {"json": {"agent_id": str(builtins["summary"].id)}}),
        (
            "patch",
            f"{base}/{note.id}",
            {"json": {"content_markdown": "x", "expected_version": 1}},
        ),
        ("post", f"{base}/{note.id}/restore", {}),
        ("delete", f"{base}/{note.id}", {}),
    ]:
        resp = await getattr(client, method)(
            url, headers=power_user_token_headers, **kwargs
        )
        assert resp.status_code == 403, f"{method} {url} -> {resp.status_code}"
