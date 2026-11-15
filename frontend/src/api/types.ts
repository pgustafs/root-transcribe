/**
 * API types mirroring the backend Pydantic schemas (see backend/app/schemas).
 */

export type Role = "admin" | "power_user" | "feedback_reviewer" | "user";

export type User = {
  id: string;
  email: string;
  full_name: string | null;
  avatar_url: string | null;
  role: Role;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

/** Identity fields are managed by Keycloak; these are application-only patches. */
export type UserAccessUpdate = { is_active: boolean };
export type UserProfileUpdate = { avatar_url?: string | null };

export type WorkspaceVisibility = "private" | "shared";
export type WorkspaceRole = "owner" | "manager" | "editor" | "viewer";

/** A workspace groups recordings and controls member access. */
export type Workspace = {
  id: string;
  name: string;
  description: string | null;
  visibility: WorkspaceVisibility;
  owner_id: string | null;
  created_by_id: string | null;
  member_count: number;
  my_role: WorkspaceRole | null;
  created_at: string;
};

export type WorkspaceMember = {
  id: string;
  user_id: string;
  email: string | null;
  role: WorkspaceRole;
  created_at: string;
};

/** One in-app notification (today: workspace-comment @mentions). */
export type AppNotification = {
  id: string;
  type: string;
  title: string;
  body: string | null;
  /** SPA path the drawer navigates to on click. */
  link: string | null;
  payload: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
};

/** Lifecycle state of a background (Celery) job. */
export type JobStatus =
  "pending" | "started" | "success" | "failure" | "retry" | "revoked";

/** One Celery task execution as shown on the admin Jobs page. */
export type Job = {
  id: string;
  task_id: string;
  task_name: string;
  status: JobStatus;
  request_id: string | null;
  triggered_by: string | null;
  worker: string | null;
  queue: string | null;
  retries: number;
  args_summary: string | null;
  result_summary: string | null;
  error: string | null;
  traceback: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  runtime_ms: number | null;
};

/** Aggregate job counts for the Jobs page header. */
export type JobSummary = {
  total: number;
  pending: number;
  started: number;
  success: number;
  failure: number;
  retry: number;
  revoked: number;
  active: boolean;
};

/** Query filters for the jobs list. */
export type JobListParams = {
  status?: JobStatus;
  task_name?: string;
  q?: string;
  skip?: number;
  limit?: number;
};

// --- Meetings (recording + STT + diarization) ---

/** One recording's transcription lifecycle. */
export type MeetingStatus =
  | "pending"
  | "uploading"
  | "transcribing"
  | "diarizing"
  | "identifying"
  | "completed"
  | "failed";

export type RecordingLanguage = "auto" | "en" | "sv";

/** A meeting recording and its transcription state (the poll payload). */
export type MeetingRecording = {
  id: string;
  workspace_id: string;
  title: string;
  status: MeetingStatus;
  current_step: string | null;
  duration_seconds: number | null;
  requested_language?: RecordingLanguage;
  language: string | null;
  size_bytes: number | null;
  /**
   * Safe, user-facing failure message from the backend. Prefer `error_code` for
   * display — `error` may be a legacy raw string and must never be rendered
   * verbatim (issue #37).
   */
  error: string | null;
  /**
   * Stable failure classification (issue #37). One of the media_validation
   * codes; null on legacy rows and non-failed recordings. The UI localizes this
   * and derives whether a retry can help.
   */
  error_code: string | null;
  participant_consent: boolean;
  audio_deleted: boolean;
  /** Sensitivity label (issue #6, E2). */
  sensitivity: "internal" | "confidential" | "restricted";
  /** Transcript revisions (issue #6). */
  latest_revision_id: string | null;
  approved_revision_id: string | null;
  created_by_email: string | null;
  created_at: string;
  updated_at: string;
};

/** One speaker turn of a diarized transcript. */
export type MeetingSegment = {
  id: string;
  /** The immutable revision this segment belongs to (issue #6). */
  revision_id: string;
  /** Optimistic-concurrency counter; send back as expected_version on PATCH. */
  version: number;
  idx: number;
  start_ms: number;
  end_ms: number;
  speaker_label: string;
  speaker_profile_id: string | null;
  speaker_name: string | null;
  speaker_confidence: number | null;
  text: string;
  is_edited: boolean;
};

/** A name the language model found for a speaker, with its evidence. */
export type SpeakerNameSuggestion = {
  speaker_label: string;
  name: string;
  /** The words from the transcript the name rests on, and where they are. */
  quote: string;
  start_ms: number;
  confidence: "high" | "medium";
  status: "pending" | "accepted" | "dismissed";
};

/** One "find the names in the transcript" run. */
export type SpeakerNameRun = {
  id: string;
  recording_id: string;
  revision_id: string;
  status: "pending" | "processing" | "ready" | "failed";
  suggestions: SpeakerNameSuggestion[];
  error: string | null;
  model: string | null;
  duration_ms: number | null;
  created_at: string;
  updated_at: string;
};

/** A recording plus its ordered transcript segments. */
export type MeetingRecordingDetail = MeetingRecording & {
  segments: MeetingSegment[];
  /** An active legal hold blocks every purge path (issue #6, E3). */
  legal_hold: boolean;
};

/** One immutable transcript revision (issue #6). */
export type MeetingRevision = {
  id: string;
  recording_id: string;
  revision_number: number;
  parent_revision_id: string | null;
  state:
    "machine_candidate" | "in_review" | "approved" | "superseded" | "discarded";
  origin: "pipeline" | "migration" | "redaction";
  attempt_id: string | null;
  /** Carry-over merge summary from the previous revision. */
  carried_edits: number;
  conflict_count: number;
  approved_by_email: string | null;
  approved_at: string | null;
  approval_note: string | null;
  created_at: string;
};

/** One open resumable multipart upload session (issue #6). */
export type MeetingUploadSession = {
  id: string;
  recording_id: string;
  /** Server-chosen fixed part size the client must honor. */
  part_size_bytes: number;
  status: "active" | "completing" | "completed" | "aborted" | "expired";
  expires_at: string;
  created_at: string;
};

/** A word or pattern a workspace wants found in its transcripts. */
export type DlpTerm = {
  id: string;
  pattern: string;
  is_regex: boolean;
  label: string | null;
  active: boolean;
  created_by_email: string | null;
  created_at: string;
};

/** One DLP detector hit awaiting review (issue #6, E4). */
export type DlpFinding = {
  id: string;
  recording_id: string;
  revision_id: string;
  segment_id: string;
  detector: "email" | "phone" | "card" | "secret" | "term" | "manual";
  term_label: string | null;
  matched_text: string;
  start_char: number;
  end_char: number;
  start_ms: number;
  end_ms: number;
  status: "pending" | "accepted" | "redacting" | "dismissed" | "redacted";
  created_at: string;
};

/** One governed audio clip of a recording (issue #6, D5). */
export type MeetingClip = {
  id: string;
  recording_id: string;
  title: string;
  start_ms: number;
  end_ms: number;
  /** Transcript revision on display when the clip was cut (provenance). */
  revision_id: string | null;
  status: "pending" | "processing" | "ready" | "failed";
  size_bytes: number | null;
  error: string | null;
  created_by_email: string | null;
  created_at: string;
  updated_at: string;
};

/** One comment on a meeting transcript (issue #6, D4). */
export type MeetingComment = {
  id: string;
  recording_id: string;
  parent_id: string | null;
  body: string;
  /** Moment of the recording the comment is anchored to (click-to-seek). */
  at_ms: number | null;
  segment_id: string | null;
  /** The transcript text it was made on (robust to later revisions). */
  anchor_quote: string | null;
  resolved_at: string | null;
  resolved_by_id: string | null;
  author_id: string | null;
  author_email: string | null;
  author_name: string | null;
  mentioned_user_ids: string[];
  created_at: string;
  updated_at: string;
};

export type SpeakerSuggestion = {
  id: string;
  recording_id: string;
  revision_id: string;
  speaker_label: string;
  suggested_profile_id: string;
  suggested_profile_name: string | null;
  score: number;
  margin: number;
  sample_turns: number;
  status: "pending" | "accepted" | "rejected";
  created_at: string;
};

/** A person in the org speaker directory (voiceprint enrollment). */
export type SpeakerProfile = {
  id: string;
  display_name: string;
  user_id: string | null;
  status: "active" | "revoked";
  consent_granted_at: string | null;
  voiceprint_count: number;
  /** Enrollment samples uploaded but not yet processed into a voiceprint —
   * erasable while pending even though voiceprint_count is still 0. */
  pending_enrollment_count?: number;
  created_by_email: string | null;
  created_at: string;
  updated_at: string;
};
