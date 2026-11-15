/** Study notes API: notebooks, sessions and captures. */

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

export type NoteTag = "important" | "unclear" | "question";
export const NOTE_TAGS: NoteTag[] = ["important", "unclear", "question"];
export type AnswerSource =
  | {
      kind: "recording";
      label: string;
      title: string;
      recording_id: string;
      start_ms: number;
    }
  | {
      kind: "note";
      label: string;
      title: string;
      notebook_id: string;
      note_id: string;
    }
  | { kind: "web"; title: string; url: string };
export type NoteTerm = { term: string; definition: string };
export type EnrichStatus =
  "none" | "pending" | "processing" | "ready" | "failed";

export type NoteContext = {
  available: boolean;
  reason: "not_linked" | "not_transcribed" | null;
  start_ms: number;
  end_ms: number;
  at_start: boolean;
  at_end: boolean;
  segments: {
    start_ms: number;
    end_ms: number;
    speaker: string | null;
    text: string;
  }[];
};

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
  /** The owner's own words, apart from the transcript. */
  comment: string | null;
  tags: NoteTag[];
  /** Written by the language model. */
  title: string | null;
  /** The transcript without fillers; `text` stays as it was said. */
  clean_text: string | null;
  /** What can be read in the image (Markdown). */
  slide_text: string | null;
  terms: NoteTerm[] | null;
  explanation: string | null;
  enrich_status: EnrichStatus;
  enrich_error: string | null;
  /** A note marked "question": what was asked, and a suggested answer. */
  question: string | null;
  answer_short: string | null;
  answer: string | null;
  answer_sources: AnswerSource[] | null;
  answer_status: EnrichStatus;
  /** "no_speech", "no_question" or a readable reason. */
  answer_error: string | null;
  end_reason: string | null;
  source_time_url: string | null;
  created_at: string;
};

export type StudyNotebookDetail = StudyNotebook & {
  sessions: StudySession[];
  notes: StudyNote[];
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
    enrich?: boolean;
    research_web?: boolean;
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
  /** Absent for a note from a microphone recording. */
  image: Blob | null;
  audio: Blob | null;
  audioStartMs: number | null;
  audioEndMs: number | null;
  endReason: string | null;
  tags?: string[];
  comment?: string | null;
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
  if (note.image) form.append("image", note.image, "frame.png");
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
  if (note.tags?.length) form.append("tags", note.tags.join(","));
  if (note.comment?.trim()) form.append("comment", note.comment.trim());
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

export async function setNoteTags(
  workspaceId: string,
  noteId: string,
  tags: NoteTag[],
): Promise<StudyNote> {
  const { data } = await apiClient.patch<StudyNote>(
    `${base(workspaceId)}/notes/${noteId}`,
    { tags },
  );
  return data;
}

export async function enrichNote(
  workspaceId: string,
  noteId: string,
): Promise<StudyNote> {
  const { data } = await apiClient.post<StudyNote>(
    `${base(workspaceId)}/notes/${noteId}/enrich`,
  );
  return data;
}

export async function getNoteContext(
  workspaceId: string,
  noteId: string,
  seconds: number,
): Promise<NoteContext> {
  const { data } = await apiClient.get<NoteContext>(
    `${base(workspaceId)}/notes/${noteId}/context`,
    { params: { seconds } },
  );
  return data;
}

export type StudyFeatures = { enrich: boolean; web_research: boolean };

export async function getFeatures(workspaceId: string): Promise<StudyFeatures> {
  const { data } = await apiClient.get<StudyFeatures>(
    `${base(workspaceId)}/features`,
  );
  return data;
}

export async function getNote(
  workspaceId: string,
  noteId: string,
): Promise<StudyNote> {
  const { data } = await apiClient.get<StudyNote>(
    `${base(workspaceId)}/notes/${noteId}`,
  );
  return data;
}

/** Look for an answer to a note's question (again). */
export async function answerNote(
  workspaceId: string,
  noteId: string,
): Promise<StudyNote> {
  const { data } = await apiClient.post<StudyNote>(
    `${base(workspaceId)}/notes/${noteId}/answer`,
  );
  return data;
}

export async function setNoteComment(
  workspaceId: string,
  noteId: string,
  comment: string,
): Promise<StudyNote> {
  const { data } = await apiClient.patch<StudyNote>(
    `${base(workspaceId)}/notes/${noteId}`,
    { comment },
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
