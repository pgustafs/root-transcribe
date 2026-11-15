/** React Query hooks for study notebooks and the keyboard-shortcut key. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import * as studyApi from "@/api/study";
import type { StudyNotebookDetail } from "@/api/study";

export const studyKeys = {
  notebooks: (workspaceId: string) =>
    ["study", workspaceId, "notebooks"] as const,
  forRecording: (workspaceId: string, recordingId: string) =>
    ["study", workspaceId, "notebooks", "recording", recordingId] as const,
  notebook: (workspaceId: string, notebookId: string) =>
    ["study", workspaceId, "notebook", notebookId] as const,
  image: (noteId: string) => ["study", "image", noteId] as const,
  credential: ["study", "hotkey-credential"] as const,
};

export function useNotebooks(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: studyKeys.notebooks(workspaceId),
    queryFn: () => studyApi.listNotebooks(workspaceId),
    enabled: enabled && Boolean(workspaceId),
  });
}

export function useRecordingNotebooks(
  workspaceId: string,
  recordingId: string,
  enabled = true,
) {
  return useQuery({
    queryKey: studyKeys.forRecording(workspaceId, recordingId),
    queryFn: () => studyApi.listNotebooks(workspaceId, recordingId),
    enabled: enabled && Boolean(workspaceId) && Boolean(recordingId),
  });
}

/** One notebook; polls while a note is still being transcribed, or while the
 * recorder is adding notes (`live`). */
export function useNotebook(
  workspaceId: string,
  notebookId: string,
  live = false,
) {
  return useQuery({
    queryKey: studyKeys.notebook(workspaceId, notebookId),
    queryFn: () => studyApi.getNotebook(workspaceId, notebookId),
    enabled: Boolean(workspaceId) && Boolean(notebookId),
    refetchInterval: (query) => {
      const data = query.state.data as StudyNotebookDetail | undefined;
      const working = data?.notes.some(
        (note) => note.status === "pending" || note.status === "processing",
      );
      return working || live ? 2000 : false;
    },
  });
}

/** A note's image as an object URL (the image route needs the bearer token,
 * so a plain <img src> cannot load it). */
export function useNoteImage(
  workspaceId: string,
  noteId: string,
  enabled: boolean,
): { url: string | null; failed: boolean } {
  const query = useQuery({
    queryKey: studyKeys.image(noteId),
    queryFn: () => studyApi.getNoteImage(workspaceId, noteId),
    enabled,
    staleTime: Infinity,
    retry: 1,
  });
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!query.data) {
      setUrl(null);
      return;
    }
    const next = URL.createObjectURL(query.data);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [query.data]);
  return { url, failed: query.isError };
}

export function useRetryNote(workspaceId: string, notebookId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (noteId: string) => studyApi.retryNote(workspaceId, noteId),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: studyKeys.notebook(workspaceId, notebookId),
      }),
  });
}

export function useSetNoteSourceTime(workspaceId: string, notebookId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { noteId: string; url: string }) =>
      studyApi.setNoteSourceTime(workspaceId, input.noteId, input.url),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: studyKeys.notebook(workspaceId, notebookId),
      }),
  });
}

export function useDeleteNote(workspaceId: string, notebookId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (noteId: string) => studyApi.deleteNote(workspaceId, noteId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["study", workspaceId] }),
    meta: { notebookId },
  });
}

export function useDeleteNotebook(workspaceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (notebookId: string) =>
      studyApi.deleteNotebook(workspaceId, notebookId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: studyKeys.notebooks(workspaceId) }),
  });
}

export function useHotkeyCredential(enabled = true) {
  return useQuery({
    queryKey: studyKeys.credential,
    queryFn: studyApi.getHotkeyCredential,
    enabled,
  });
}

export function useCreateHotkeyCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: studyApi.createHotkeyCredential,
    onSuccess: (created) =>
      qc.setQueryData(studyKeys.credential, { ...created, token: null }),
  });
}

export function useRevokeHotkeyCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: studyApi.revokeHotkeyCredential,
    onSuccess: () =>
      qc.setQueryData(studyKeys.credential, {
        exists: false,
        hint: null,
        created_at: null,
        last_used_at: null,
        token: null,
      }),
  });
}

const HINT_KEY = "root_transcribe.study.shortcut";

/** The key combination the learner says they assigned (this browser only). */
export function readShortcutHint(): string {
  try {
    return localStorage.getItem(HINT_KEY) ?? "";
  } catch {
    return "";
  }
}

export function writeShortcutHint(value: string): void {
  try {
    if (value.trim()) localStorage.setItem(HINT_KEY, value.trim());
    else localStorage.removeItem(HINT_KEY);
    window.dispatchEvent(new Event("study-shortcut-hint"));
  } catch {
    // Private mode: the hint simply is not remembered.
  }
}

export function useShortcutHint(): string {
  const [hint, setHint] = useState(readShortcutHint);
  useEffect(() => {
    const update = () => setHint(readShortcutHint());
    window.addEventListener("study-shortcut-hint", update);
    return () => window.removeEventListener("study-shortcut-hint", update);
  }, []);
  return hint;
}
