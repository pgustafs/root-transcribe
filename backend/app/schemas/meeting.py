"""Pydantic contracts for meeting recordings + transcripts.

Response models never expose the raw audio-store key or any biometric data — a
segment carries only the resolved ``speaker_name`` (looked up from the directory),
not embeddings. See ARCHITECTURE.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RecordingLanguage = Literal["auto", "en", "sv"]


class MeetingRecordingPublic(BaseModel):
    """A recording row + its transcription lifecycle (the poll payload)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    status: str
    current_step: str | None = None
    duration_seconds: float | None = None
    requested_language: RecordingLanguage = "auto"
    language: str | None = None
    size_bytes: int | None = None
    # ``error`` is a safe, user-facing message; ``error_code`` is the stable
    # classification the UI localizes and uses to tell the user whether a retry
    # can help (issue #37). NULL on legacy rows — the UI falls back safely.
    error: str | None = None
    error_code: str | None = None
    participant_consent: bool = False
    audio_deleted: bool = False
    # Transcript revisions: newest candidate and approved authoritative version.
    latest_revision_id: uuid.UUID | None = None
    approved_revision_id: uuid.UUID | None = None
    # internal | confidential | restricted (issue #6, E2)
    sensitivity: str = "internal"
    created_by_email: str | None = None
    created_at: datetime
    updated_at: datetime


class MeetingSegmentPublic(BaseModel):
    """One speaker turn of the transcript."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    revision_id: uuid.UUID
    # Optimistic-concurrency counter; send it back as ``expected_version``.
    version: int = 1
    idx: int
    start_ms: int
    end_ms: int
    speaker_label: str
    speaker_profile_id: uuid.UUID | None = None
    # Resolved from the directory by the route; None ⇒ anonymous speaker.
    speaker_name: str | None = None
    speaker_confidence: float | None = None
    text: str
    is_edited: bool = False


class MeetingRecordingDetail(MeetingRecordingPublic):
    """A recording plus its ordered transcript segments."""

    segments: list[MeetingSegmentPublic] = Field(default_factory=list)
    # An active legal hold blocks every purge path (issue #6, E3).
    legal_hold: bool = False


class MeetingSegmentUpdate(BaseModel):
    """Human correction to a segment: edit text and/or (re)assign the speaker.

    ``speaker_profile_id`` set ⇒ assign that person; explicitly ``null`` with
    ``clear_speaker`` ⇒ make the segment anonymous again. ``text`` set ⇒ edit.
    """

    text: str | None = Field(default=None, max_length=20_000)
    speaker_profile_id: uuid.UUID | None = None
    clear_speaker: bool = False
    # Optimistic concurrency: when set, the edit is refused (409) if the
    # segment has changed since this version was read.
    expected_version: int | None = Field(default=None, ge=1)
    # Optional human note recorded on the immutable edit event.
    reason: str | None = Field(default=None, max_length=500)


class MeetingSpeakerAssign(BaseModel):
    """Assign (or clear) a person for EVERY segment carrying one diarization
    label — the "I recognize Speaker 1, that's Bob" bulk correction.

    This is a human annotation only: it never derives or stores a voiceprint
    from the meeting audio (that would be biometric processing needing the
    speaker's own explicit consent — see ARCHITECTURE.md).
    """

    speaker_label: str = Field(min_length=1, max_length=64)
    speaker_profile_id: uuid.UUID | None = None
    clear_speaker: bool = False


class MeetingRevisionPublic(BaseModel):
    """One immutable transcript revision (issue #6)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    revision_number: int
    parent_revision_id: uuid.UUID | None = None
    # machine_candidate | in_review | approved | superseded | discarded
    state: str
    # pipeline | migration | redaction
    origin: str
    attempt_id: uuid.UUID | None = None
    carried_edits: int = 0
    conflict_count: int = 0
    approved_by_email: str | None = None
    approved_at: datetime | None = None
    approval_note: str | None = None
    created_at: datetime


class MeetingRevisionApprove(BaseModel):
    """Approval payload — an optional reviewer note for the audit trail."""

    note: str | None = Field(default=None, max_length=1000)


class SpeakerSuggestionPublic(BaseModel):
    """One uncertain speaker match awaiting human confirmation (issue #6)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    revision_id: uuid.UUID
    speaker_label: str
    suggested_profile_id: uuid.UUID
    # Resolved by the route from the directory.
    suggested_profile_name: str | None = None
    score: float
    margin: float
    sample_turns: int
    status: str
    created_at: datetime


class MeetingUploadSessionCreate(BaseModel):
    """Open a resumable multipart upload session (issue #6)."""

    title: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    requested_language: RecordingLanguage = "auto"
    content_type: str | None = Field(default=None, max_length=100)
    # Declared total size (may be unknown for live recordings) — bounded by
    # AUDIO_MAX_BYTES at completion either way.
    size_bytes: int | None = Field(default=None, ge=1)
    participant_consent: bool = False


class MeetingUploadSessionPublic(BaseModel):
    """The open session: ids + the part size the client must honor."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    part_size_bytes: int
    status: str
    expires_at: datetime
    created_at: datetime


class MeetingUploadPartManifest(BaseModel):
    """One part of the completion manifest."""

    part_number: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)


#: Container/codec are CLOSED enums, not patterned strings (issue #37 review
#: round 6). A MIME-shaped regex still accepts arbitrary values —
#: ``secret/topsecret`` and ``audio/webm;token=secret`` both look like media
#: types — so the client normalises to these tokens and anything else is
#: ``unknown``. Nothing caller-chosen reaches the operator log.
MeetingMediaContainer = Literal[
    "webm", "ogg", "mp4", "mp3", "wav", "flac", "aac", "unknown"
]
MeetingMediaCodec = Literal["opus", "vorbis", "aac", "mp3", "pcm", "flac", "unknown"]

#: Browser families we recognise. A CLOSED set, because this value is written
#: to the operator log — a free-text field there is a log-injection and
#: data-exfiltration channel, not a diagnostic (issue #37 review round 5).
MeetingBrowserFamily = Literal[
    "Chrome", "Firefox", "Safari", "Edge", "Opera", "unknown"
]


class MeetingClientDiagnostics(BaseModel):
    """Privacy-safe recorder context captured with the upload (issue #37).

    Persisted on the recording so the WORKER can attach it to the media
    rejection it already emits. That is what makes the rejection signal
    trustworthy: the browser reports *context* at upload time, but only the
    worker decides — and reports — that media was rejected, so a client cannot
    forge or replay a verdict (review round 7).
    """

    model_config = ConfigDict(extra="forbid")

    browser_family: MeetingBrowserFamily = "unknown"
    browser_major: int | None = Field(default=None, ge=0, le=999)
    container: MeetingMediaContainer | None = None
    codec: MeetingMediaCodec | None = None
    chunk_count: int | None = Field(default=None, ge=0, le=1_000_000)
    missing_count: int | None = Field(default=None, ge=0, le=1_000_000)
    duplicate_count: int | None = Field(default=None, ge=0, le=1_000_000)
    #: Largest WALL-CLOCK gap between timeslices — includes client latency.
    max_gap_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    #: Largest gap in the recorder's own timecodes — a stalled RECORDER.
    max_media_gap_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    #: |wall − media| — how far client handling lagged the recorder.
    max_skew_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    total_bytes: int | None = Field(default=None, ge=0, le=1_000_000_000_000)


class MeetingUploadComplete(BaseModel):
    """Explicit completion: the client's manifest of every uploaded part."""

    parts: list[MeetingUploadPartManifest] = Field(min_length=1)
    #: Optional recorder context stored with the recording (issue #37 r7) so a
    #: later worker-side media rejection can be correlated to a browser.
    client_diagnostics: MeetingClientDiagnostics | None = None


#: Stable client-recorder event categories (issue #37). Deliberately a CLOSED
#: set: it is the metric's label value, so it must stay low-cardinality, and it
#: keeps free-text out of an endpoint the browser can call.
#:
#: NOTE there is no ``media_rejected`` here (review round 7). Only the worker
#: knows whether media was rejected, so only the worker reports it — a
#: client-submitted verdict would be forgeable (any member could attribute one
#: to someone else's recording) and replayable (every reload re-sending it).
#: These are exactly the events the SERVER cannot observe: media the browser
#: refused before upload, local assembly faults, and recovery outcomes.
MeetingClientEventName = Literal[
    "preflight_empty",
    "preflight_too_small",
    "preflight_bad_header",
    "assembly_incomplete",
    "assembly_duplicate",
    "upload_recovered",
    "upload_recovery_failed",
]


class MeetingClientEvent(BaseModel):
    """A privacy-safe browser-side recorder diagnostic (issue #37).

    Every field is a closed enum or a bounded integer — there is no free-text
    field at all, so nothing caller-chosen can reach the operator log — unknown
    fields are rejected, and it carries NO audio, transcript, title, or object
    key. It also carries no verdict about a recording: see the note on
    :data:`MeetingClientEventName`.
    """

    model_config = ConfigDict(extra="forbid")

    event: MeetingClientEventName
    browser_family: MeetingBrowserFamily = "unknown"
    #: Major version only — enough to correlate with a browser release.
    browser_major: int | None = Field(default=None, ge=0, le=999)
    #: The recorded/selected container + codec, normalised to closed tokens.
    container: MeetingMediaContainer | None = None
    codec: MeetingMediaCodec | None = None
    #: How many timeslices the recorder had produced (assembly diagnostics).
    chunk_count: int | None = Field(default=None, ge=0, le=1_000_000)
    #: Bounded assembly/timing aggregates (issue #37 review round 5): enough to
    #: answer "did the browser emit badly, or did we assemble badly?" without
    #: shipping per-chunk data.
    missing_count: int | None = Field(default=None, ge=0, le=1_000_000)
    duplicate_count: int | None = Field(default=None, ge=0, le=1_000_000)
    max_gap_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    max_media_gap_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    max_skew_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    total_bytes: int | None = Field(default=None, ge=0, le=1_000_000_000_000)


class MeetingPermissionsPublic(BaseModel):
    """The caller's effective audio capabilities in a workspace (issue #6)."""

    capabilities: list[str] = Field(default_factory=list)


class MeetingGrantSet(BaseModel):
    """Upsert one per-workspace capability override (role XOR user scoped)."""

    capability: str = Field(min_length=1, max_length=40)
    allow: bool
    role: str | None = Field(default=None, pattern="^(owner|manager|editor|viewer)$")
    user_id: uuid.UUID | None = None


class MeetingGrantPublic(BaseModel):
    """One stored capability override."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    role: str | None = None
    user_id: uuid.UUID | None = None
    capability: str
    allow: bool
    created_by_email: str | None = None
    created_at: datetime


class MeetingSensitivitySet(BaseModel):
    """Change a recording's sensitivity label (approver-gated)."""

    sensitivity: str = Field(pattern="^(internal|confidential|restricted)$")


class MeetingHoldSet(BaseModel):
    """Place a legal hold on a recording."""

    reason: str = Field(min_length=1, max_length=500)


class RetentionPolicySet(BaseModel):
    """Upsert one per-artifact workspace retention override."""

    artifact: str = Field(min_length=1, max_length=30)
    # None = keep indefinitely (an explicit choice).
    retention_days: int | None = Field(default=None, ge=1)


class RetentionPolicyPublic(BaseModel):
    """One stored retention override."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope_type: str
    scope_id: uuid.UUID | None = None
    artifact: str
    retention_days: int | None = None
    created_by_email: str | None = None
    created_at: datetime


class MeetingCommentCreate(BaseModel):
    """Comment on a transcript (root, or a reply via ``parent_id``).

    ``at_ms``/``segment_id`` anchor the comment to a moment of the recording;
    ``anchor_quote`` carries the transcript text it was made on (robust to
    later revisions). ``mentions`` are workspace-member user ids to @-notify.
    """

    body: str = Field(min_length=1, max_length=8000)
    parent_id: uuid.UUID | None = None
    at_ms: int | None = Field(default=None, ge=0)
    segment_id: uuid.UUID | None = None
    anchor_quote: str | None = Field(default=None, max_length=2000)
    mentions: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class MeetingCommentUpdate(BaseModel):
    """Edit a comment's body (author only)."""

    body: str = Field(min_length=1, max_length=8000)


class MeetingCommentPublic(BaseModel):
    """A transcript comment as the UI sees it (author denormalized)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    body: str
    at_ms: int | None = None
    segment_id: uuid.UUID | None = None
    anchor_quote: str | None = None
    resolved_at: datetime | None = None
    resolved_by_id: uuid.UUID | None = None
    author_id: uuid.UUID | None = None
    author_email: str | None = None
    author_name: str | None = None
    mentioned_user_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MeetingClipCreate(BaseModel):
    """Cut one excerpt of the recording into its own governed clip."""

    title: str = Field(min_length=1, max_length=255)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class MeetingClipPublic(BaseModel):
    """A clip: job state while slicing, playable artifact once ready."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    title: str
    start_ms: int
    end_ms: int
    revision_id: uuid.UUID | None = None
    status: str
    size_bytes: int | None = None
    error: str | None = None
    created_by_email: str | None = None
    created_at: datetime
    updated_at: datetime


class DlpTermCreate(BaseModel):
    """One workspace-defined DLP pattern (literal, or regex if flagged)."""

    pattern: str = Field(min_length=1, max_length=500)
    is_regex: bool = False
    label: str | None = Field(default=None, max_length=100)


class DlpTermPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pattern: str
    is_regex: bool
    label: str | None = None
    active: bool
    created_by_email: str | None = None
    created_at: datetime


class DlpFindingPublic(BaseModel):
    """One detector hit awaiting review (or its redaction record)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    recording_id: uuid.UUID
    revision_id: uuid.UUID
    segment_id: uuid.UUID
    detector: str
    term_label: str | None = None
    matched_text: str
    start_char: int
    end_char: int
    start_ms: int
    end_ms: int
    status: str
    created_at: datetime


class DlpFindingStatusSet(BaseModel):
    """Review decision on one finding (redacted is set by the worker only)."""

    status: str = Field(pattern="^(pending|accepted|dismissed)$")


class DlpManualFinding(BaseModel):
    """Manually mark a span of one segment for redaction."""

    segment_id: uuid.UUID
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
