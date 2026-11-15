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
NoteTag = Literal["important", "unclear", "question"]


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
    # Let the language model work on the notes (when one is configured).
    enrich: bool = True
    # Questions may be researched on the web (only the question is sent out).
    research_web: bool = False
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
    enrich: bool = True
    research_web: bool = False
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
    comment: str | None
    tags: list[str] = Field(default_factory=list)
    title: str | None = None
    clean_text: str | None = None
    slide_text: str | None = None
    terms: list[dict[str, str]] | None = None
    explanation: str | None = None
    enrich_status: str = "none"
    enrich_error: str | None = None
    question: str | None = None
    answer_short: str | None = None
    answer: str | None = None
    answer_sources: list[dict] | None = None
    answer_status: str = "none"
    answer_error: str | None = None
    end_reason: str | None
    source_time_url: str | None
    created_at: datetime


class NoteUpdate(BaseModel):
    """Only the fields that are sent are changed."""

    # Empty string removes the link.
    source_time_url: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None
    # The owner's own words. Empty string removes them.
    comment: Annotated[str, StringConstraints(max_length=10000)] | None = None
    # The whole set of marks on the note.
    tags: list[NoteTag] | None = None


class NotebookDetail(NotebookPublic):
    sessions: list[SessionPublic] = Field(default_factory=list)
    notes: list[NotePublic] = Field(default_factory=list)


class ContextSegment(BaseModel):
    start_ms: int
    end_ms: int
    speaker: str | None = None
    text: str


class NoteContext(BaseModel):
    """What was said around a note, from the full recording's transcript."""

    available: bool
    # not_linked | not_transcribed | no_access
    reason: str | None = None
    start_ms: int = 0
    end_ms: int = 0
    # Nothing more exists before / after this range.
    at_start: bool = False
    at_end: bool = False
    segments: list[ContextSegment] = Field(default_factory=list)


class StudyFeatures(BaseModel):
    # A language model is configured: notes can be worked on.
    enrich: bool
    # Web search is set up: questions can be researched on the web.
    web_research: bool = False
