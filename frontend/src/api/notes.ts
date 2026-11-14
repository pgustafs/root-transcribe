/** Meeting notes: agents (instructions) and the documents they write. */

import { apiClient } from "@/api/client";

export type NoteAgent = {
  id: string;
  name: string;
  description: string | null;
  instructions: string;
  is_active: boolean;
  /** Shared with everyone (true) or the caller's own (false). */
  shared: boolean;
  builtin: boolean;
  /** Built-ins only: the text differs from the shipped default. */
  modified: boolean;
  can_edit: boolean;
  updated_at: string;
};

export type NoteStatus = "pending" | "processing" | "ready" | "failed";

export type MeetingNote = {
  id: string;
  recording_id: string;
  agent_id: string | null;
  agent_name: string;
  /** The transcript version the note was written from. */
  revision_id: string | null;
  status: NoteStatus;
  /** write | part:2/4 | merge while the agent works. */
  current_step: string | null;
  content_markdown: string | null;
  version: number;
  edited: boolean;
  edited_by_email: string | null;
  edited_at: string | null;
  error: string | null;
  model: string | null;
  duration_ms: number | null;
  created_by_email: string | null;
  created_at: string;
  updated_at: string;
};

export type NoteExportFormat = "md" | "docx" | "pdf";

export async function listNoteAgents(): Promise<NoteAgent[]> {
  const { data } = await apiClient.get<NoteAgent[]>("/note-agents");
  return data;
}

export async function createNoteAgent(input: {
  name: string;
  description: string | null;
  instructions: string;
  shared: boolean;
}): Promise<NoteAgent> {
  const { data } = await apiClient.post<NoteAgent>("/note-agents", input);
  return data;
}

export async function updateNoteAgent(
  id: string,
  input: Partial<{
    name: string;
    description: string | null;
    instructions: string;
    is_active: boolean;
  }>,
): Promise<NoteAgent> {
  const { data } = await apiClient.patch<NoteAgent>(
    `/note-agents/${id}`,
    input,
  );
  return data;
}

export async function resetNoteAgent(id: string): Promise<NoteAgent> {
  const { data } = await apiClient.post<NoteAgent>(`/note-agents/${id}/reset`);
  return data;
}

export async function deleteNoteAgent(id: string): Promise<void> {
  await apiClient.delete(`/note-agents/${id}`);
}

const notesUrl = (workspaceId: string, recordingId: string) =>
  `/workspaces/${workspaceId}/recordings/${recordingId}/notes`;

export async function listNotes(
  workspaceId: string,
  recordingId: string,
): Promise<MeetingNote[]> {
  const { data } = await apiClient.get<MeetingNote[]>(
    notesUrl(workspaceId, recordingId),
  );
  return data;
}

export async function createNote(
  workspaceId: string,
  recordingId: string,
  agentId: string,
): Promise<MeetingNote> {
  const { data } = await apiClient.post<MeetingNote>(
    notesUrl(workspaceId, recordingId),
    { agent_id: agentId },
  );
  return data;
}

export async function updateNote(
  workspaceId: string,
  recordingId: string,
  noteId: string,
  input: { content_markdown: string; expected_version: number },
): Promise<MeetingNote> {
  const { data } = await apiClient.patch<MeetingNote>(
    `${notesUrl(workspaceId, recordingId)}/${noteId}`,
    input,
  );
  return data;
}

export async function restoreNote(
  workspaceId: string,
  recordingId: string,
  noteId: string,
): Promise<MeetingNote> {
  const { data } = await apiClient.post<MeetingNote>(
    `${notesUrl(workspaceId, recordingId)}/${noteId}/restore`,
  );
  return data;
}

export async function deleteNote(
  workspaceId: string,
  recordingId: string,
  noteId: string,
): Promise<void> {
  await apiClient.delete(`${notesUrl(workspaceId, recordingId)}/${noteId}`);
}

export async function exportNote(
  workspaceId: string,
  recordingId: string,
  noteId: string,
  format: NoteExportFormat,
): Promise<{ blob: Blob; filename: string | null }> {
  const response = await apiClient.get(
    `${notesUrl(workspaceId, recordingId)}/${noteId}/export`,
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
