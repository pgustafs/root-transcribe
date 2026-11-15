"""Request and response shapes for study notes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
Url = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
StudyLanguage = Literal["auto", "en", "sv"]
StudyMode = Literal["until_stopped", "last_15s"]


class NotebookCreate(BaseModel):
    title: Title
    source_url: Url | None = None


class NotebookUpdate(BaseModel):
    title: Title | None = None
    source_url: Url | None = None


class NotebookPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    source_url: str | None
    source_key: str | None
    note_count: int = 0
    created_at: datetime
    updated_at: datetime


class SessionCreate(BaseModel):
    notebook_id: uuid.UUID
    language: StudyLanguage = "auto"
    mode: StudyMode = "until_stopped"
    started_at: datetime | None = None


class SessionUpdate(BaseModel):
    recording_id: uuid.UUID


class SessionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notebook_id: uuid.UUID
    recording_id: uuid.UUID | None
    language: str
    mode: str
    started_at: datetime


class NotePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notebook_id: uuid.UUID
    session_id: uuid.UUID
    seq: int
    captured_ms: int
    audio_start_ms: int | None
    audio_end_ms: int | None
    has_image: bool = False
    image_width: int | None
    image_height: int | None
    has_audio: bool
    status: str
    text: str | None
    error: str | None
    end_reason: str | None
    source_time_url: str | None
    created_at: datetime


class NoteUpdate(BaseModel):
    # Empty string removes the link.
    source_time_url: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=2000)
    ]


class NotebookDetail(NotebookPublic):
    sessions: list[SessionPublic] = Field(default_factory=list)
    notes: list[NotePublic] = Field(default_factory=list)


class HotkeyCredentialPublic(BaseModel):
    exists: bool
    hint: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    # Only in the answer to "create": the secret is never shown again.
    token: str | None = None


class CommandAck(BaseModel):
    outcome: Literal["started", "saved", "error"]
    # paused | sharing_lost | stale | storage | not_recording | busy
    reason: Annotated[str, StringConstraints(max_length=40)] | None = None
    captured_ms: int | None = Field(default=None, ge=0)
    audio_start_ms: int | None = Field(default=None, ge=0)
    audio_end_ms: int | None = Field(default=None, ge=0)
    has_audio: bool = True
    # Milliseconds from the press reaching the server to the frame being pinned.
    delay_ms: int | None = Field(default=None, ge=0)
