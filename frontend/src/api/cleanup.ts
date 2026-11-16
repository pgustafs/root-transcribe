/** The cleaned copy of a transcript: the language model's corrections, kept
 * beside the transcript (which is never changed). */
import { apiClient } from "@/api/client";

/** One corrected line. `dismissed` = a person chose the original. */
export type CleanupChange = {
  segment_id: string;
  /** The line as the model read it. */
  original: string;
  text: string;
  status: "suggested" | "dismissed";
};

export type TranscriptCleanup = {
  id: string;
  recording_id: string;
  revision_id: string;
  status: "pending" | "processing" | "ready" | "failed";
  /** "part:2/7" while the model reads the transcript. */
  current_step: string | null;
  changes: CleanupChange[];
  /** Proposals left out because they rewrote a line instead of correcting it. */
  rejected: number;
  error: string | null;
  model: string | null;
  duration_ms: number | null;
  created_at: string;
  updated_at: string;
};

const base = (workspaceId: string, recordingId: string) =>
  `/workspaces/${workspaceId}/recordings/${recordingId}/cleanup`;

/** The newest cleanup of the transcript on display, or null. */
export async function getCleanup(
  workspaceId: string,
  recordingId: string,
): Promise<TranscriptCleanup | null> {
  const { data } = await apiClient.get<TranscriptCleanup | null>(
    base(workspaceId, recordingId),
  );
  return data;
}

/** Ask the language model to correct the transcript (replaces an earlier one). */
export async function createCleanup(
  workspaceId: string,
  recordingId: string,
): Promise<TranscriptCleanup> {
  const { data } = await apiClient.post<TranscriptCleanup>(
    base(workspaceId, recordingId),
  );
  return data;
}

/** Keep the original line (`dismiss`) or take the correction back (`use`). */
export async function decideCleanupChange(
  workspaceId: string,
  recordingId: string,
  cleanupId: string,
  segmentId: string,
  decision: "use" | "dismiss",
): Promise<TranscriptCleanup> {
  const { data } = await apiClient.post<TranscriptCleanup>(
    `${base(workspaceId, recordingId)}/${cleanupId}/changes/${segmentId}`,
    { decision },
  );
  return data;
}

export async function deleteCleanup(
  workspaceId: string,
  recordingId: string,
  cleanupId: string,
): Promise<void> {
  await apiClient.delete(`${base(workspaceId, recordingId)}/${cleanupId}`);
}
