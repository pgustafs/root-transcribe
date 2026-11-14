"""Audio-specific capability layer for meetings (issue #6, wave E1).

Workspace membership alone is too coarse for audio: an enterprise needs to let
someone read a transcript without downloading the raw recording, or edit
wording without approving revisions. This module defines the capability
vocabulary, a code-level default map from the effective workspace role
(``viewer``/``editor``/``manager``/``owner``, plus global ADMIN), and a
resolver that applies per-workspace ``meeting_permission_grant`` overrides
(role- or user-scoped allow/deny rows, user rows winning).

Deliberately NOT a policy engine: one enum, one default dict, one override
table — enforced in the API handlers (never UI-only) via
``require_capability``.
"""

from __future__ import annotations

import enum
import uuid

from app.models.user import Role, User


class MeetingCapability(enum.StrEnum):
    """Independently grantable meeting/audio operations."""

    RECORD = "record"
    UPLOAD = "upload"
    LISTEN = "listen"
    DOWNLOAD = "download"
    VIEW_TRANSCRIPT = "view_transcript"
    EDIT_TRANSCRIPT = "edit_transcript"
    APPROVE_TRANSCRIPT = "approve_transcript"
    RELABEL_SPEAKERS = "relabel_speakers"
    EXPORT = "export"
    CREATE_CLIP = "create_clip"
    DELETE = "delete"
    REDACT = "redact"
    LEGAL_HOLD = "legal_hold"
    MANAGE_RETENTION = "manage_retention"


_VIEWER = {
    MeetingCapability.LISTEN,
    MeetingCapability.VIEW_TRANSCRIPT,
}
_EDITOR = _VIEWER | {
    MeetingCapability.RECORD,
    MeetingCapability.UPLOAD,
    MeetingCapability.EDIT_TRANSCRIPT,
    MeetingCapability.RELABEL_SPEAKERS,
    MeetingCapability.CREATE_CLIP,
}
_MANAGER = _EDITOR | {
    MeetingCapability.APPROVE_TRANSCRIPT,
    MeetingCapability.EXPORT,
    MeetingCapability.DOWNLOAD,
    MeetingCapability.DELETE,
}

DEFAULT_CAPABILITIES: dict[str, frozenset[MeetingCapability]] = {
    "viewer": frozenset(_VIEWER),
    "editor": frozenset(_EDITOR),
    "manager": frozenset(_MANAGER),
    "owner": frozenset(_MANAGER),
}


def base_capabilities(
    *, user: User, effective_role: str | None
) -> set[MeetingCapability]:
    """The pre-override capability set for a user in a workspace."""
    if user.role == Role.ADMIN:
        return set(MeetingCapability)
    if effective_role is None:
        return set()
    return set(DEFAULT_CAPABILITIES.get(effective_role, frozenset()))


def apply_grants(
    capabilities: set[MeetingCapability],
    grants: list,
    *,
    user_id: uuid.UUID,
    effective_role: str | None,
) -> set[MeetingCapability]:
    """Apply ``meeting_permission_grant`` rows: role rows first, then user rows
    (a user-scoped row overrides a role-scoped one for the same capability)."""
    result = set(capabilities)
    for scope_user in (False, True):  # role-scoped first, user-scoped last
        for grant in grants:
            if (grant.user_id is not None) is not scope_user:
                continue
            if scope_user:
                if grant.user_id != user_id:
                    continue
            elif grant.role != effective_role:
                continue
            try:
                capability = MeetingCapability(grant.capability)
            except ValueError:
                continue  # unknown capability rows are inert, never fatal
            if grant.allow:
                result.add(capability)
            else:
                result.discard(capability)
    return result
