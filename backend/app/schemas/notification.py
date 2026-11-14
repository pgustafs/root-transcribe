"""Schemas for global in-app notifications."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NotificationPublic(BaseModel):
    """One notification as the drawer sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    body: str | None = None
    # SPA path the drawer navigates to on click.
    link: str | None = None
    payload: dict = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime


class NotificationUnreadCount(BaseModel):
    """The masthead badge's unread total."""

    count: int
