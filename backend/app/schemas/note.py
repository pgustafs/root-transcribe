"""Schemas for note agents and meeting notes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

INSTRUCTIONS_MAX = 12000
NOTE_MAX = 200_000


class NoteAgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    instructions: str = Field(min_length=1, max_length=INSTRUCTIONS_MAX)
    # True = shared with everyone (admins and power users only).
    shared: bool = False


class NoteAgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(
        default=None, min_length=1, max_length=INSTRUCTIONS_MAX
    )
    is_active: bool | None = None


class NoteAgentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    instructions: str
    is_active: bool
    # Shared with everyone (True) or the caller's own (False).
    shared: bool
    builtin: bool
    # Built-ins only: the text differs from the shipped default.
    modified: bool = False
    # Whether the caller may change or delete it.
    can_edit: bool
    updated_at: datetime


class MeetingNoteCreate(BaseModel):
    agent_id: uuid.UUID


class MeetingNoteUpdate(BaseModel):
    content_markdown: str = Field(max_length=NOTE_MAX)
    # The version the editor started from; a mismatch means someone else saved.
    expected_version: int


class MeetingNotePublic(BaseModel):
    """A note: job state while the agent writes, a document once ready."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    agent_name: str
    revision_id: uuid.UUID | None = None
    status: str
    current_step: str | None = None
    content_markdown: str | None = None
    version: int
    edited: bool = False
    edited_by_email: str | None = None
    edited_at: datetime | None = None
    error: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    created_by_email: str | None = None
    created_at: datetime
    updated_at: datetime


class SpeakerNameSuggestion(BaseModel):
    """A name the language model found for one speaker, with its evidence."""

    speaker_label: str
    name: str
    # The words from the transcript the name rests on, and where they are.
    quote: str
    start_ms: int
    confidence: Literal["high", "medium"]
    status: Literal["pending", "accepted", "dismissed"]


class SpeakerNameRunPublic(BaseModel):
    """One "find the names in the transcript" run."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    revision_id: uuid.UUID
    status: str
    suggestions: list[SpeakerNameSuggestion] = Field(default_factory=list)
    error: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    created_at: datetime
    updated_at: datetime


class SpeakerNameDecision(BaseModel):
    decision: Literal["accept", "dismiss"]
