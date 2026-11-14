/** React Query hooks for note agents and meeting notes. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as notesApi from "@/api/notes";
import type { MeetingNote } from "@/api/notes";

const agentKeys = ["note-agents"] as const;
const noteKeys = (workspaceId: string, recordingId: string) =>
  ["meetings", workspaceId, recordingId, "notes"] as const;

export function useNoteAgents(enabled = true) {
  return useQuery({
    queryKey: agentKeys,
    queryFn: notesApi.listNoteAgents,
    enabled,
  });
}

function useAgentMutation<TInput, TResult>(
  mutationFn: (input: TInput) => Promise<TResult>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSettled: () => qc.invalidateQueries({ queryKey: agentKeys }),
  });
}

export const useCreateNoteAgent = () =>
  useAgentMutation(notesApi.createNoteAgent);

export const useUpdateNoteAgent = () =>
  useAgentMutation(
    ({
      id,
      ...input
    }: { id: string } & Parameters<typeof notesApi.updateNoteAgent>[1]) =>
      notesApi.updateNoteAgent(id, input),
  );

export const useResetNoteAgent = () =>
  useAgentMutation(notesApi.resetNoteAgent);

export const useDeleteNoteAgent = () =>
  useAgentMutation(notesApi.deleteNoteAgent);

/** A recording's notes; polls while an agent is writing one. */
export function useNotes(workspaceId: string, recordingId: string) {
  return useQuery({
    queryKey: noteKeys(workspaceId, recordingId),
    queryFn: () => notesApi.listNotes(workspaceId, recordingId),
    enabled: Boolean(workspaceId) && Boolean(recordingId),
    refetchInterval: (query) =>
      (query.state.data as MeetingNote[] | undefined)?.some(
        (n) => n.status === "pending" || n.status === "processing",
      )
        ? 2000
        : false,
  });
}

function useNoteMutation<TInput, TResult>(
  workspaceId: string,
  recordingId: string,
  mutationFn: (input: TInput) => Promise<TResult>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSettled: () =>
      qc.invalidateQueries({ queryKey: noteKeys(workspaceId, recordingId) }),
  });
}

export const useCreateNote = (workspaceId: string, recordingId: string) =>
  useNoteMutation(workspaceId, recordingId, (agentId: string) =>
    notesApi.createNote(workspaceId, recordingId, agentId),
  );

export const useUpdateNote = (workspaceId: string, recordingId: string) =>
  useNoteMutation(
    workspaceId,
    recordingId,
    (input: {
      noteId: string;
      content_markdown: string;
      expected_version: number;
    }) =>
      notesApi.updateNote(workspaceId, recordingId, input.noteId, {
        content_markdown: input.content_markdown,
        expected_version: input.expected_version,
      }),
  );

export const useRestoreNote = (workspaceId: string, recordingId: string) =>
  useNoteMutation(workspaceId, recordingId, (noteId: string) =>
    notesApi.restoreNote(workspaceId, recordingId, noteId),
  );

export const useDeleteNote = (workspaceId: string, recordingId: string) =>
  useNoteMutation(workspaceId, recordingId, (noteId: string) =>
    notesApi.deleteNote(workspaceId, recordingId, noteId),
  );
