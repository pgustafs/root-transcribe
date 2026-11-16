"""Schemas for the cleaned copy of a transcript."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CleanupChange(BaseModel):
    """One corrected line: the transcript segment, the line as the model read
    it, and its proposal. ``dismissed`` = a person chose the original."""

    segment_id: uuid.UUID
    original: str
    text: str
    status: Literal["suggested", "dismissed"] = "suggested"


class TranscriptCleanupPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    revision_id: uuid.UUID
    status: str
    current_step: str | None = None
    changes: list[CleanupChange] = Field(default_factory=list)
    # Proposals dropped because they rewrote a line instead of correcting it.
    rejected: int = 0
    error: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    created_at: datetime
    updated_at: datetime


class CleanupDecision(BaseModel):
    """``dismiss`` keeps the original line; ``use`` takes the correction back."""

    decision: Literal["use", "dismiss"]
