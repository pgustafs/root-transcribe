/** Meeting recording API: upload, poll, review transcripts and export revisions. */

import { apiClient } from "@/api/client";
import type {
  AudioLayout,
  DlpFinding,
  DlpTerm,
  SpeakerNameRun,
  MeetingClip,
  MeetingComment,
  MeetingRecording,
  RecordingLanguage,
  MeetingRecordingDetail,
  MeetingRevision,
  MeetingSegment,
  MeetingUploadSession,
  SpeakerSuggestion,
} from "@/api/types";
import type {
  BrowserFamily,
  MediaCodec,
  MediaContainer,
} from "@/lib/clientDiagnostics";

/** Keyset cursor: the previous page's last row (stable under concurrent
 * inserts/deletes, unlike offsets — review round 4). */
export type RecordingsCursor = { createdAt: string; id: string };

export async function listRecordings(
  workspaceId: string,
  limit = 100,
  cursor: RecordingsCursor | null = null,
): Promise<MeetingRecording[]> {
  const { data } = await apiClient.get<MeetingRecording[]>(
    `/workspaces/${workspaceId}/recordings`,
    {
      params: {
        limit,
        ...(cursor
          ? { before_created_at: cursor.createdAt, before_id: cursor.id }
          : {}),
      },
    },
  );
  return data;
}

export async function getRecording(
  workspaceId: string,
  recordingId: string,
  options: { signal?: AbortSignal } = {},
): Promise<MeetingRecordingDetail> {
  const { data } = await apiClient.get<MeetingRecordingDetail>(
    `/workspaces/${workspaceId}/recordings/${recordingId}`,
    { signal: options.signal },
  );
  return data;
}

/** Stable browser-side recorder event categories (issue #37) — mirrors
 * `MeetingClientEventName` on the backend. */
export type MeetingClientEventName =
  | "preflight_empty"
  | "preflight_too_small"
  | "preflight_bad_header"
  | "assembly_incomplete"
  | "assembly_duplicate"
  | "upload_recovered"
  | "upload_recovery_failed";

/**
 * Report a privacy-safe recorder diagnostic so operators can see client-side
 * failures (issue #37). BEST EFFORT: diagnostics must never interrupt the user,
 * so every error — including being offline — is swallowed.
 */
/** Fields every event carries. Container/codec are CLOSED tokens, never a
 * caller-chosen MIME string (issue #37 review round 6). */
type MeetingClientEventBase = {
  browser_family?: BrowserFamily;
  browser_major?: number | null;
  container?: MediaContainer | null;
  codec?: MediaCodec | null;
  chunk_count?: number | null;
  missing_count?: number | null;
  duplicate_count?: number | null;
  max_gap_ms?: number | null;
  max_media_gap_ms?: number | null;
  max_skew_ms?: number | null;
  total_bytes?: number | null;
};

/**
 * A discriminated union mirroring the backend's event-shape rule: only
 * `media_rejected` carries correlation, and it REQUIRES both fields — the
 * server verifies them against the recording row before counting or logging.
 */
export type MeetingClientEventPayload = MeetingClientEventBase & {
  event: MeetingClientEventName;
};

/** Recorder context sent WITH the upload (issue #37 r7). The worker attaches it
 * to any media rejection it emits, so a rejection can be correlated to a
 * browser without the client ever asserting a verdict. */
export type MeetingClientDiagnostics = MeetingClientEventBase;

export async function reportClientEvent(
  workspaceId: string,
  payload: MeetingClientEventPayload,
): Promise<void> {
  try {
    await apiClient.post(
      `/workspaces/${workspaceId}/meetings/client-events`,
      payload,
    );
  } catch {
    // Telemetry is never worth surfacing to the user.
  }
}

export async function createRecording(
  workspaceId: string,
  input: {
    file: Blob;
    filename: string;
    title: string;
    consent: boolean;
    requestedLanguage?: RecordingLanguage;
    /** How many people speak; null/undefined lets speaker detection decide. */
    expectedSpeakers?: number | null;
    /** Same closed-token context the resumable path sends (issue #37 r8), so a
     * picker upload the worker later rejects still carries attribution. */
    clientDiagnostics?: MeetingClientDiagnostics;
  },
): Promise<MeetingRecording> {
  const form = new FormData();
  form.append("file", input.file, input.filename);
  form.append("title", input.title);
  form.append("participant_consent", String(input.consent));
  form.append("requested_language", input.requestedLanguage ?? "auto");
  if (input.expectedSpeakers)
    form.append("expected_speakers", String(input.expectedSpeakers));
  if (input.clientDiagnostics)
    form.append("client_diagnostics", JSON.stringify(input.clientDiagnostics));
  // Clear the JSON default so the platform sets multipart/form-data + boundary.
  const { data } = await apiClient.post<MeetingRecording>(
    `/workspaces/${workspaceId}/recordings`,
    form,
    { headers: { "Content-Type": undefined } },
  );
  return data;
}

export async function retryRecording(
  workspaceId: string,
  recordingId: string,
  /** Change how many people speak (null: detect); a count left out is kept.
   * `micSpeakers` is the microphone side of an online-meeting recording. */
  options?: { expectedSpeakers?: number | null; micSpeakers?: number | null },
): Promise<MeetingRecording> {
  const fields: Record<string, number | null> = {};
  if (options && "expectedSpeakers" in options)
    fields.expected_speakers = options.expectedSpeakers ?? null;
  if (options && "micSpeakers" in options)
    fields.mic_speakers = options.micSpeakers ?? null;
  const body = Object.keys(fields).length > 0 ? fields : undefined;
  const { data } = await apiClient.post<MeetingRecording>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/retry`,
    body,
  );
  return data;
}

export async function updateSegment(
  workspaceId: string,
  recordingId: string,
  segmentId: string,
  patch: {
    text?: string;
    speaker_profile_id?: string | null;
    clear_speaker?: boolean;
    /** Optimistic concurrency (issue #6): 409 when the segment changed. */
    expected_version?: number;
    reason?: string;
  },
): Promise<MeetingSegment> {
  const { data } = await apiClient.patch<MeetingSegment>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/segments/${segmentId}`,
    patch,
  );
  return data;
}

export async function deleteRecording(
  workspaceId: string,
  recordingId: string,
): Promise<void> {
  await apiClient.delete(
    `/workspaces/${workspaceId}/recordings/${recordingId}`,
  );
}

/** Authenticated blob URL for in-app playback (the API proxies the object store). */

/** Assign (or clear) a person for EVERY segment with one diarization label —
 * the bulk "I recognize Speaker 1, that's Bob" correction. Annotation only:
 * never derives a voiceprint from the meeting audio. */
export async function assignSpeakerByLabel(
  workspaceId: string,
  recordingId: string,
  input: {
    speaker_label: string;
    speaker_profile_id?: string | null;
    /** A typed name for this recording only (not added to the directory). */
    speaker_name?: string;
    clear_speaker?: boolean;
  },
): Promise<{ updated_segments: number }> {
  const { data } = await apiClient.post<{ updated_segments: number }>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/assign-speaker`,
    input,
  );
  return data;
}

/** The newest name search on the current transcript, or null. */
export async function getSpeakerNameRun(
  workspaceId: string,
  recordingId: string,
): Promise<SpeakerNameRun | null> {
  const { data } = await apiClient.get<SpeakerNameRun | null>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/speaker-names`,
  );
  return data;
}

/** Ask the language model to find the speakers' names in what was said. */
export async function identifySpeakerNames(
  workspaceId: string,
  recordingId: string,
): Promise<SpeakerNameRun> {
  const { data } = await apiClient.post<SpeakerNameRun>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/speaker-names/identify`,
  );
  return data;
}

export async function decideSpeakerName(
  workspaceId: string,
  recordingId: string,
  runId: string,
  index: number,
  decision: "accept" | "dismiss",
): Promise<SpeakerNameRun> {
  const { data } = await apiClient.post<SpeakerNameRun>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/speaker-names/${runId}/suggestions/${index}`,
    { decision },
  );
  return data;
}

export async function listSpeakerSuggestions(
  workspaceId: string,
  recordingId: string,
): Promise<SpeakerSuggestion[]> {
  const { data } = await apiClient.get<SpeakerSuggestion[]>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/speaker-suggestions`,
  );
  return data;
}

export async function acceptSpeakerSuggestion(
  workspaceId: string,
  recordingId: string,
  suggestionId: string,
): Promise<{ updated_segments: number }> {
  const { data } = await apiClient.post<{ updated_segments: number }>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/speaker-suggestions/${suggestionId}/accept`,
  );
  return data;
}

export async function rejectSpeakerSuggestion(
  workspaceId: string,
  recordingId: string,
  suggestionId: string,
): Promise<void> {
  await apiClient.post(
    `/workspaces/${workspaceId}/recordings/${recordingId}/speaker-suggestions/${suggestionId}/reject`,
  );
}

// --- Resumable multipart uploads (issue #6, wave B4) ---

export async function createUploadSession(
  workspaceId: string,
  input: {
    title: string;
    filename: string;
    size_bytes?: number;
    participant_consent: boolean;
    requested_language?: RecordingLanguage;
    expected_speakers?: number | null;
    audio_layout?: AudioLayout;
    mic_speakers?: number | null;
  },
): Promise<MeetingUploadSession> {
  const { data } = await apiClient.post<MeetingUploadSession>(
    `/workspaces/${workspaceId}/meetings/uploads`,
    input,
  );
  return data;
}

export async function uploadSessionPart(
  workspaceId: string,
  sessionId: string,
  partNumber: number,
  body: ArrayBuffer,
  sha256: string,
  // Cancellation must reach the PART upload too, not just completion: a part
  // PUT over a stalled connection can stay pending indefinitely, and without
  // this the recorder bar disappeared while the upload carried on and the
  // server never received the DELETE (issue #37 review round 14).
  signal?: AbortSignal,
): Promise<void> {
  await apiClient.put(
    `/workspaces/${workspaceId}/meetings/uploads/${sessionId}/parts/${partNumber}`,
    body,
    {
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Part-Sha256": sha256,
      },
      signal,
    },
  );
}

export async function completeUploadSession(
  workspaceId: string,
  sessionId: string,
  parts: { part_number: number; sha256: string }[],
  clientDiagnostics?: MeetingClientDiagnostics,
  // Cancellation has to reach the request ITSELF, not just the gaps between
  // retries: a completion POST can be pending for a long time, and without
  // this the client accepted its 202 — and moved the recording on — after the
  // user had already cancelled (issue #37 review round 13).
  signal?: AbortSignal,
): Promise<MeetingRecording> {
  const { data } = await apiClient.post<MeetingRecording>(
    `/workspaces/${workspaceId}/meetings/uploads/${sessionId}/complete`,
    // Diagnostics ride WITH the upload (issue #37 r7) so the worker can attach
    // them to any media rejection it emits — the client never sends a verdict.
    {
      parts,
      ...(clientDiagnostics ? { client_diagnostics: clientDiagnostics } : {}),
    },
    { signal },
  );
  return data;
}

export async function abortUploadSession(
  workspaceId: string,
  sessionId: string,
): Promise<void> {
  await apiClient.delete(
    `/workspaces/${workspaceId}/meetings/uploads/${sessionId}`,
  );
}

/** Cancellation the caller can actually TRUST.
 *
 * The server refuses cancel while a completion holds a fresh lease (409) — the
 * upload may still finish. Swallowing that and reporting success made the UI
 * delete the user's only local copy of a recording the server was in the middle
 * of completing (issue #37 review round 13). `true` means the server confirmed
 * the session is cancelled; `false` means it is NOT, for any reason. */
export async function tryAbortUploadSession(
  workspaceId: string,
  sessionId: string,
): Promise<boolean> {
  try {
    await abortUploadSession(workspaceId, sessionId);
    return true;
  } catch (error) {
    // A 404 IS confirmation: the session does not exist, so there is nothing
    // left to cancel. Reporting "unconfirmed" here kept a client whose 204 was
    // lost retrying against ids the server had already destroyed (issue #37
    // review round 14).
    return (
      (error as { response?: { status?: number } })?.response?.status === 404
    );
  }
}

// --- Streaming playback (issue #6, wave B6) ---

/** Mint a short-lived playback URL for the <audio> element: authorization
 * happens server-side at mint time; the token then lets the media element
 * stream and SEEK with HTTP Range requests — no whole-recording Blob. */
export async function createPlaybackSession(
  workspaceId: string,
  recordingId: string,
): Promise<{ url: string }> {
  const { data } = await apiClient.post<{ url: string }>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/audio/session`,
  );
  return data;
}

/** Explicit download via the independently authorized download endpoint. */
export async function downloadRecordingAudio(
  workspaceId: string,
  recordingId: string,
): Promise<{ blob: Blob; filename: string | null }> {
  const response = await apiClient.get(
    `/workspaces/${workspaceId}/recordings/${recordingId}/audio/download`,
    { responseType: "blob" },
  );
  const disposition = String(
    response.headers["content-disposition"] ?? "",
  ).match(/filename="([^"]+)"/);
  return {
    blob: response.data as Blob,
    filename: disposition ? disposition[1] : null,
  };
}

// --- Transcript revisions (issue #6, wave C2) ---

export async function listRevisions(
  workspaceId: string,
  recordingId: string,
): Promise<MeetingRevision[]> {
  const { data } = await apiClient.get<MeetingRevision[]>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/revisions`,
  );
  return data;
}

export async function getRevisionSegments(
  workspaceId: string,
  recordingId: string,
  revisionId: string,
): Promise<MeetingSegment[]> {
  const { data } = await apiClient.get<MeetingSegment[]>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/revisions/${revisionId}/segments`,
  );
  return data;
}

export async function approveRevision(
  workspaceId: string,
  recordingId: string,
  revisionId: string,
  note?: string,
): Promise<MeetingRevision> {
  const { data } = await apiClient.post<MeetingRevision>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/revisions/${revisionId}/approve`,
    { note: note || null },
  );
  return data;
}

export async function discardRevision(
  workspaceId: string,
  recordingId: string,
  revisionId: string,
): Promise<MeetingRevision> {
  const { data } = await apiClient.post<MeetingRevision>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/revisions/${revisionId}/discard`,
  );
  return data;
}

/** Download one revision in an export format (issue #6, C3). */
export async function exportRevision(
  workspaceId: string,
  recordingId: string,
  revisionId: string,
  format: "md" | "txt" | "srt" | "vtt" | "docx" | "pdf",
): Promise<{ blob: Blob; filename: string | null }> {
  const response = await apiClient.get(
    `/workspaces/${workspaceId}/recordings/${recordingId}/revisions/${revisionId}/export`,
    { params: { format }, responseType: "blob" },
  );
  const disposition = String(
    response.headers["content-disposition"] ?? "",
  ).match(/filename="([^"]+)"/);
  return {
    blob: response.data as Blob,
    filename: disposition ? disposition[1] : null,
  };
}

/** The caller's server-computed audio capabilities (issue #6, E1). */
export async function getMeetingPermissions(
  workspaceId: string,
): Promise<{ capabilities: string[] }> {
  const { data } = await apiClient.get<{ capabilities: string[] }>(
    `/workspaces/${workspaceId}/meetings/permissions`,
  );
  return data;
}

/** Change the sensitivity label (approver-gated; issue #6, E2). */
export async function setRecordingSensitivity(
  workspaceId: string,
  recordingId: string,
  sensitivity: MeetingRecording["sensitivity"],
): Promise<MeetingRecording> {
  const { data } = await apiClient.patch<MeetingRecording>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/sensitivity`,
    { sensitivity },
  );
  return data;
}

/** Transcript comments (issue #6, D4). */
export async function listMeetingComments(
  workspaceId: string,
  recordingId: string,
  includeResolved: boolean,
): Promise<MeetingComment[]> {
  const { data } = await apiClient.get<MeetingComment[]>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/comments`,
    { params: { include_resolved: includeResolved } },
  );
  return data;
}

export async function addMeetingComment(
  workspaceId: string,
  recordingId: string,
  input: {
    body: string;
    parent_id?: string;
    at_ms?: number;
    segment_id?: string;
    anchor_quote?: string;
    mentions?: string[];
  },
): Promise<MeetingComment> {
  const { data } = await apiClient.post<MeetingComment>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/comments`,
    input,
  );
  return data;
}

export async function updateMeetingComment(
  workspaceId: string,
  recordingId: string,
  commentId: string,
  body: string,
): Promise<MeetingComment> {
  const { data } = await apiClient.patch<MeetingComment>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/comments/${commentId}`,
    { body },
  );
  return data;
}

export async function deleteMeetingComment(
  workspaceId: string,
  recordingId: string,
  commentId: string,
): Promise<void> {
  await apiClient.delete(
    `/workspaces/${workspaceId}/recordings/${recordingId}/comments/${commentId}`,
  );
}

export async function resolveMeetingComment(
  workspaceId: string,
  recordingId: string,
  commentId: string,
  resolved: boolean,
): Promise<MeetingComment> {
  const { data } = await apiClient.post<MeetingComment>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/comments/${commentId}/` +
      (resolved ? "resolve" : "reopen"),
  );
  return data;
}

/** Governed audio clips (issue #6, D5). */
export async function listClips(
  workspaceId: string,
  recordingId: string,
): Promise<MeetingClip[]> {
  const { data } = await apiClient.get<MeetingClip[]>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/clips`,
  );
  return data;
}

export async function createClip(
  workspaceId: string,
  recordingId: string,
  input: { title: string; start_ms: number; end_ms: number },
): Promise<MeetingClip> {
  const { data } = await apiClient.post<MeetingClip>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/clips`,
    input,
  );
  return data;
}

export async function deleteClip(
  workspaceId: string,
  recordingId: string,
  clipId: string,
): Promise<void> {
  await apiClient.delete(
    `/workspaces/${workspaceId}/recordings/${recordingId}/clips/${clipId}`,
  );
}

export async function createClipPlaybackSession(
  workspaceId: string,
  recordingId: string,
  clipId: string,
): Promise<{ url: string }> {
  const { data } = await apiClient.post<{ url: string }>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/clips/${clipId}/audio/session`,
  );
  return data;
}

/** The workspace's own words and patterns to look for. */
export async function listDlpTerms(workspaceId: string): Promise<DlpTerm[]> {
  const { data } = await apiClient.get<DlpTerm[]>(
    `/workspaces/${workspaceId}/meetings/dlp-terms`,
  );
  return data;
}

export async function addDlpTerm(
  workspaceId: string,
  input: { pattern: string; is_regex: boolean; label: string | null },
): Promise<DlpTerm> {
  const { data } = await apiClient.post<DlpTerm>(
    `/workspaces/${workspaceId}/meetings/dlp-terms`,
    input,
  );
  return data;
}

export async function deleteDlpTerm(
  workspaceId: string,
  termId: string,
): Promise<void> {
  await apiClient.delete(
    `/workspaces/${workspaceId}/meetings/dlp-terms/${termId}`,
  );
}

/** DLP review & redaction (issue #6, E4). */
export async function listDlpFindings(
  workspaceId: string,
  recordingId: string,
): Promise<DlpFinding[]> {
  const { data } = await apiClient.get<DlpFinding[]>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/dlp`,
  );
  return data;
}

export async function setDlpFindingStatus(
  workspaceId: string,
  recordingId: string,
  findingId: string,
  status: "pending" | "accepted" | "dismissed",
): Promise<DlpFinding> {
  const { data } = await apiClient.post<DlpFinding>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/dlp/findings/${findingId}/status`,
    { status },
  );
  return data;
}

export async function addManualDlpFinding(
  workspaceId: string,
  recordingId: string,
  input: { segment_id: string; start_char: number; end_char: number },
): Promise<DlpFinding> {
  const { data } = await apiClient.post<DlpFinding>(
    `/workspaces/${workspaceId}/recordings/${recordingId}/dlp/findings`,
    input,
  );
  return data;
}

export async function requestDlpScan(
  workspaceId: string,
  recordingId: string,
): Promise<void> {
  await apiClient.post(
    `/workspaces/${workspaceId}/recordings/${recordingId}/dlp/scan`,
  );
}

export async function applyRedaction(
  workspaceId: string,
  recordingId: string,
): Promise<void> {
  await apiClient.post(
    `/workspaces/${workspaceId}/recordings/${recordingId}/redact`,
  );
}
