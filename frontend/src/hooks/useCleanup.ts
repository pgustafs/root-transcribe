import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as cleanupApi from "@/api/cleanup";
import type { TranscriptCleanup } from "@/api/cleanup";

const cleanupKey = (workspaceId: string, recordingId: string) =>
  ["meetings", workspaceId, recordingId, "cleanup"] as const;

/** The cleaned copy of the transcript on display; polls while it is made. */
export function useCleanup(
  workspaceId: string,
  recordingId: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: cleanupKey(workspaceId, recordingId),
    queryFn: () => cleanupApi.getCleanup(workspaceId, recordingId),
    enabled: enabled && Boolean(workspaceId) && Boolean(recordingId),
    refetchInterval: (query) => {
      const status = (query.state.data as TranscriptCleanup | null | undefined)
        ?.status;
      return status === "pending" || status === "processing" ? 2000 : false;
    },
  });
}

export function useCreateCleanup(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => cleanupApi.createCleanup(workspaceId, recordingId),
    onSuccess: (cleanup) =>
      qc.setQueryData(cleanupKey(workspaceId, recordingId), cleanup),
  });
}

/** Keep the original line, or take the correction back. */
export function useDecideCleanupChange(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      cleanupId: string;
      segmentId: string;
      decision: "use" | "dismiss";
    }) =>
      cleanupApi.decideCleanupChange(
        workspaceId,
        recordingId,
        input.cleanupId,
        input.segmentId,
        input.decision,
      ),
    onSuccess: (cleanup) =>
      qc.setQueryData(cleanupKey(workspaceId, recordingId), cleanup),
  });
}

export function useDeleteCleanup(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (cleanupId: string) =>
      cleanupApi.deleteCleanup(workspaceId, recordingId, cleanupId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: cleanupKey(workspaceId, recordingId) }),
  });
}
