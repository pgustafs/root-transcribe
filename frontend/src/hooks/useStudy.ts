/** React Query hooks for study notebooks. */

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
        (note) =>
          note.status === "pending" ||
          note.status === "processing" ||
          note.enrich_status === "pending" ||
          note.enrich_status === "processing",
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

/** What this installation can do with notes. */
export function useStudyFeatures(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["study", workspaceId, "features"] as const,
    queryFn: () => studyApi.getFeatures(workspaceId),
    enabled,
    staleTime: 5 * 60 * 1000,
  });
}

function useNoteMutation<Input>(
  workspaceId: string,
  notebookId: string,
  run: (input: Input) => Promise<studyApi.StudyNote>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: (saved) =>
      qc.setQueryData<StudyNotebookDetail>(
        studyKeys.notebook(workspaceId, notebookId),
        (current) =>
          current && {
            ...current,
            notes: current.notes.map((note) =>
              note.id === saved.id ? saved : note,
            ),
          },
      ),
  });
}

export function useSetNoteTags(workspaceId: string, notebookId: string) {
  return useNoteMutation(
    workspaceId,
    notebookId,
    (input: { noteId: string; tags: studyApi.NoteTag[] }) =>
      studyApi.setNoteTags(workspaceId, input.noteId, input.tags),
  );
}

export function useEnrichNote(workspaceId: string, notebookId: string) {
  return useNoteMutation(workspaceId, notebookId, (noteId: string) =>
    studyApi.enrichNote(workspaceId, noteId),
  );
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

export function useSetNoteComment(workspaceId: string, notebookId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { noteId: string; comment: string }) =>
      studyApi.setNoteComment(workspaceId, input.noteId, input.comment),
    onSuccess: (saved) =>
      // Put the saved text in place; a refetch would fight with typing.
      qc.setQueryData<StudyNotebookDetail>(
        studyKeys.notebook(workspaceId, notebookId),
        (current) =>
          current && {
            ...current,
            notes: current.notes.map((note) =>
              note.id === saved.id ? saved : note,
            ),
          },
      ),
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
