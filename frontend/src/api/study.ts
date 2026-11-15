/** Study notes API: notebooks, sessions, captures and the keyboard shortcut. */

import { apiClient } from "@/api/client";

export type StudyNotebook = {
  id: string;
  workspace_id: string;
  title: string;
  source_url: string | null;
  source_key: string | null;
  note_count: number;
  created_at: string;
  updated_at: string;
};

export type StudySession = {
  id: string;
  notebook_id: string;
  recording_id: string | null;
  language: string;
  mode: string;
  started_at: string;
};

export type StudyNoteStatus =
  | "pending"
  | "processing"
  | "ready"
  | "no_speech"
  | "failed"
  | "screenshot_only";

export type StudyNote = {
  id: string;
  notebook_id: string;
  session_id: string;
  seq: number;
  captured_ms: number;
  audio_start_ms: number | null;
  audio_end_ms: number | null;
  has_image: boolean;
  image_width: number | null;
  image_height: number | null;
  has_audio: boolean;
  status: StudyNoteStatus;
  text: string | null;
  error: string | null;
  end_reason: string | null;
  source_time_url: string | null;
  created_at: string;
};

export type StudyNotebookDetail = StudyNotebook & {
  sessions: StudySession[];
  notes: StudyNote[];
};

export type HotkeyCredential = {
  exists: boolean;
  hint: string | null;
  created_at: string | null;
  last_used_at: string | null;
  /** Only in the answer to "create". */
  token: string | null;
};

const base = (workspaceId: string) => `/workspaces/${workspaceId}/study`;

export async function listNotebooks(
  workspaceId: string,
  recordingId?: string,
): Promise<StudyNotebook[]> {
  const { data } = await apiClient.get<StudyNotebook[]>(
    `${base(workspaceId)}/notebooks`,
    { params: recordingId ? { recording_id: recordingId } : {} },
  );
  return data;
}

/** Creates the notebook, or returns the one this source already has. */
export async function createNotebook(
  workspaceId: string,
  input: { title: string; source_url?: string | null },
): Promise<StudyNotebook> {
  const { data } = await apiClient.post<StudyNotebook>(
    `${base(workspaceId)}/notebooks`,
    input,
  );
  return data;
}

export async function getNotebook(
  workspaceId: string,
  notebookId: string,
): Promise<StudyNotebookDetail> {
  const { data } = await apiClient.get<StudyNotebookDetail>(
    `${base(workspaceId)}/notebooks/${notebookId}`,
  );
  return data;
}

export async function updateNotebook(
  workspaceId: string,
  notebookId: string,
  input: { title?: string; source_url?: string },
): Promise<StudyNotebook> {
  const { data } = await apiClient.patch<StudyNotebook>(
    `${base(workspaceId)}/notebooks/${notebookId}`,
    input,
  );
  return data;
}

export async function deleteNotebook(
  workspaceId: string,
  notebookId: string,
): Promise<void> {
  await apiClient.delete(`${base(workspaceId)}/notebooks/${notebookId}`);
}

export async function exportNotebook(
  workspaceId: string,
  notebookId: string,
): Promise<Blob> {
  const { data } = await apiClient.get<Blob>(
    `${base(workspaceId)}/notebooks/${notebookId}/export`,
    { responseType: "blob" },
  );
  return data;
}

export async function putSession(
  workspaceId: string,
  sessionId: string,
  input: {
    notebook_id: string;
    language: string;
    mode: string;
    started_at: string;
  },
): Promise<StudySession> {
  const { data } = await apiClient.put<StudySession>(
    `${base(workspaceId)}/sessions/${sessionId}`,
    input,
  );
  return data;
}

export async function linkSession(
  workspaceId: string,
  sessionId: string,
  recordingId: string,
): Promise<void> {
  await apiClient.patch(`${base(workspaceId)}/sessions/${sessionId}`, {
    recording_id: recordingId,
  });
}

export async function deleteSession(
  workspaceId: string,
  sessionId: string,
): Promise<void> {
  await apiClient.delete(`${base(workspaceId)}/sessions/${sessionId}`);
}

export type NoteUpload = {
  sessionId: string;
  seq: number;
  capturedMs: number;
  image: Blob;
  audio: Blob | null;
  audioStartMs: number | null;
  audioEndMs: number | null;
  endReason: string | null;
};

/** Send one capture. Repeating it is safe: the id decides. */
export async function putNote(
  workspaceId: string,
  noteId: string,
  note: NoteUpload,
): Promise<StudyNote> {
  const form = new FormData();
  form.append("session_id", note.sessionId);
  form.append("seq", String(note.seq));
  form.append("captured_ms", String(Math.max(0, Math.round(note.capturedMs))));
  form.append("image", note.image, "frame.png");
  if (note.audio && note.audioStartMs !== null && note.audioEndMs !== null) {
    form.append("audio", note.audio, "note.wav");
    form.append(
      "audio_start_ms",
      String(Math.max(0, Math.round(note.audioStartMs))),
    );
    form.append(
      "audio_end_ms",
      String(Math.max(0, Math.round(note.audioEndMs))),
    );
  }
  if (note.endReason) form.append("end_reason", note.endReason);
  const { data } = await apiClient.put<StudyNote>(
    `${base(workspaceId)}/notes/${noteId}`,
    form,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return data;
}

export async function retryNote(
  workspaceId: string,
  noteId: string,
): Promise<StudyNote> {
  const { data } = await apiClient.post<StudyNote>(
    `${base(workspaceId)}/notes/${noteId}/retry`,
  );
  return data;
}

export async function setNoteSourceTime(
  workspaceId: string,
  noteId: string,
  url: string,
): Promise<StudyNote> {
  const { data } = await apiClient.patch<StudyNote>(
    `${base(workspaceId)}/notes/${noteId}`,
    { source_time_url: url },
  );
  return data;
}

export async function deleteNote(
  workspaceId: string,
  noteId: string,
): Promise<void> {
  await apiClient.delete(`${base(workspaceId)}/notes/${noteId}`);
}

export async function getNoteImage(
  workspaceId: string,
  noteId: string,
): Promise<Blob> {
  const { data } = await apiClient.get<Blob>(
    `${base(workspaceId)}/notes/${noteId}/image`,
    { responseType: "blob" },
  );
  return data;
}

export async function getHotkeyCredential(): Promise<HotkeyCredential> {
  const { data } = await apiClient.get<HotkeyCredential>(
    "/study/hotkey/credential",
  );
  return data;
}

export async function createHotkeyCredential(): Promise<HotkeyCredential> {
  const { data } = await apiClient.post<HotkeyCredential>(
    "/study/hotkey/credential",
  );
  return data;
}

export async function revokeHotkeyCredential(): Promise<void> {
  await apiClient.delete("/study/hotkey/credential");
}

/** Press or test the shortcut from the page (what the Mac shortcut sends). */
export async function sendHotkey(
  token: string,
  kind: "press" | "test",
): Promise<{ ok: boolean; text: string }> {
  const prefix = apiClient.defaults.baseURL ?? "/api/v1";
  const response = await fetch(
    `${prefix}/study/hotkey${kind === "test" ? "/test" : ""}`,
    { method: "POST", headers: { Authorization: `Bearer ${token}` } },
  );
  return { ok: response.ok, text: (await response.text()).trim() };
}

export async function acknowledgeCommand(
  invocation: string,
  result: Record<string, unknown>,
): Promise<void> {
  await apiClient.post(
    `/study/commands/${encodeURIComponent(invocation)}/ack`,
    result,
  );
}

/** The address the keyboard shortcut posts to, as this browser reaches the app. */
export function hotkeyUrl(kind: "press" | "test" = "press"): string {
  const prefix = apiClient.defaults.baseURL ?? "/api/v1";
  const absolute = new URL(prefix, window.location.origin).toString();
  return `${absolute.replace(/\/$/, "")}/study/hotkey${kind === "test" ? "/test" : ""}`;
}
