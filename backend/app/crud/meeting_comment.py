"""Data access for meeting transcript comments (issue #6, D4).

Comments use one-level threads. Resolved roots hide their replies. Reply deletion
is explicit so behavior is consistent when FK cascades are unavailable in tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.meeting_comment import MeetingComment
from app.models.user import User


async def create(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    recording_id: uuid.UUID,
    author: User,
    body: str,
    parent_id: uuid.UUID | None = None,
    at_ms: int | None = None,
    segment_id: uuid.UUID | None = None,
    anchor_quote: str | None = None,
    mentioned_user_ids: list[uuid.UUID] | None = None,
) -> MeetingComment:
    comment = MeetingComment(
        workspace_id=workspace_id,
        recording_id=recording_id,
        parent_id=parent_id,
        body=body,
        at_ms=at_ms,
        segment_id=segment_id,
        anchor_quote=anchor_quote,
        author_id=author.id,
        author_email=author.email,
        author_name=author.full_name,
        mentioned_user_ids=[str(uid) for uid in (mentioned_user_ids or [])],
    )
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return comment


async def get(
    session: AsyncSession, *, workspace_id: uuid.UUID, comment_id: uuid.UUID
) -> MeetingComment:
    comment = await session.get(MeetingComment, comment_id)
    if comment is None or comment.workspace_id != workspace_id:
        raise NotFoundError("Comment not found")
    return comment


async def list_for_recording(
    session: AsyncSession,
    *,
    recording_id: uuid.UUID,
    include_resolved: bool = False,
) -> Sequence[MeetingComment]:
    """A recording's comments, oldest first (threads assembled client-side).

    Unless ``include_resolved``, resolved root comments and their replies are
    hidden.
    """
    result = await session.execute(
        select(MeetingComment)
        .where(MeetingComment.recording_id == recording_id)
        .order_by(MeetingComment.created_at)
    )
    comments = list(result.scalars().all())
    if include_resolved:
        return comments
    resolved_roots = {
        c.id for c in comments if c.parent_id is None and c.resolved_at is not None
    }
    return [
        c
        for c in comments
        if c.id not in resolved_roots and c.parent_id not in resolved_roots
    ]


async def update_body(
    session: AsyncSession, comment: MeetingComment, *, body: str
) -> MeetingComment:
    comment.body = body
    await session.commit()
    await session.refresh(comment)
    return comment


async def set_resolved(
    session: AsyncSession,
    comment: MeetingComment,
    *,
    resolved: bool,
    actor: User,
) -> MeetingComment:
    comment.resolved_at = datetime.now(UTC) if resolved else None
    comment.resolved_by_id = actor.id if resolved else None
    await session.commit()
    await session.refresh(comment)
    return comment


async def delete(session: AsyncSession, comment: MeetingComment) -> None:
    """Delete a comment; a root's replies go with it (explicit — SQLite tests
    don't enforce the FK cascade)."""
    if comment.parent_id is None:
        replies = await session.execute(
            select(MeetingComment).where(MeetingComment.parent_id == comment.id)
        )
        for reply in replies.scalars():
            await session.delete(reply)
    await session.delete(comment)
    await session.commit()
