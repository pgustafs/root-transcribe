/** React Query hooks for meeting recordings + transcripts. */

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import * as meetingsApi from "@/api/meetings";
import type {
  MeetingClip,
  MeetingStatus,
  RecordingLanguage,
  SpeakerNameRun,
} from "@/api/types";

const TERMINAL: MeetingStatus[] = ["completed", "failed"];
const isActive = (s?: MeetingStatus) => Boolean(s) && !TERMINAL.includes(s!);

export const meetingKeys = {
  list: (workspaceId: string) => ["meetings", workspaceId] as const,
  detail: (workspaceId: string, id: string) =>
    ["meetings", workspaceId, id] as const,
};

/** A workspace's recordings; polls while any recording is still processing. */
const RECORDINGS_PAGE_SIZE = 100;

export function useRecordings(workspaceId: string, enabled = true) {
  return useInfiniteQuery({
    queryKey: [...meetingKeys.list(workspaceId), "pages"],
    queryFn: ({ pageParam }) =>
      meetingsApi.listRecordings(workspaceId, RECORDINGS_PAGE_SIZE, pageParam),
    initialPageParam: null as meetingsApi.RecordingsCursor | null,
    // Keyset pages of 100 (review round 4): the cursor is the last row seen,
    // so a recording created or deleted mid-pagination can't duplicate a
    // boundary row or shift one out of view the way offsets did. A short
    // page means the end was reached.
    getNextPageParam: (lastPage) => {
      if (lastPage.length < RECORDINGS_PAGE_SIZE) return undefined;
      const last = lastPage[lastPage.length - 1];
      return { createdAt: last.created_at, id: last.id };
    },
    enabled: enabled && Boolean(workspaceId),
    refetchInterval: (query) =>
      query.state.data?.pages.flat().some((r) => isActive(r.status))
        ? 2000
        : false,
  });
}

/** One recording + its transcript; polls while it is still processing. */
export function useRecording(
  workspaceId: string,
  recordingId: string | null,
  enabled = true,
) {
  return useQuery({
    queryKey: meetingKeys.detail(workspaceId, recordingId ?? ""),
    queryFn: () => meetingsApi.getRecording(workspaceId, recordingId!),
    enabled: enabled && Boolean(workspaceId) && Boolean(recordingId),
    refetchInterval: (query) =>
      isActive(query.state.data?.status) ? 1500 : false,
  });
}

export function useCreateRecording(workspaceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      file: Blob;
      filename: string;
      title: string;
      consent: boolean;
      requestedLanguage?: RecordingLanguage;
      expectedSpeakers?: number | null;
      clientDiagnostics?: meetingsApi.MeetingClientDiagnostics;
    }) => meetingsApi.createRecording(workspaceId, input),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: meetingKeys.list(workspaceId) }),
  });
}

export function useRetryRecording(workspaceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (
      input:
        | string
        | {
            recordingId: string;
            expectedSpeakers: number | null;
            /** Only for an online-meeting recording. */
            micSpeakers?: number | null;
          },
    ) =>
      typeof input === "string"
        ? meetingsApi.retryRecording(workspaceId, input)
        : meetingsApi.retryRecording(workspaceId, input.recordingId, {
            expectedSpeakers: input.expectedSpeakers,
            ...("micSpeakers" in input
              ? { micSpeakers: input.micSpeakers ?? null }
              : {}),
          }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: meetingKeys.list(workspaceId) }),
  });
}

export function useUpdateSegment(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      segmentId: string;
      text?: string;
      speaker_profile_id?: string | null;
      clear_speaker?: boolean;
      expected_version?: number;
      reason?: string;
    }) =>
      meetingsApi.updateSegment(workspaceId, recordingId, input.segmentId, {
        text: input.text,
        speaker_profile_id: input.speaker_profile_id,
        clear_speaker: input.clear_speaker,
        expected_version: input.expected_version,
        reason: input.reason,
      }),
    // Refresh on BOTH outcomes: a 409 conflict means the transcript moved on —
    // show the current truth so the user re-applies against it (issue #6).
    onSettled: () =>
      qc.invalidateQueries({
        queryKey: meetingKeys.detail(workspaceId, recordingId),
      }),
  });
}

/** Shared key so a caller can ask "is a save running for THIS recording?" —
 * `useIsMutating` reads the mutation cache, which outlives any one component's
 * observer (issue #38). */

export function useDeleteRecording(workspaceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (recordingId: string) =>
      meetingsApi.deleteRecording(workspaceId, recordingId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: meetingKeys.list(workspaceId) }),
  });
}

/** Bulk-assign a person to every segment carrying one diarization label. */
export function useAssignSpeakerByLabel(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      speaker_label: string;
      speaker_profile_id?: string | null;
      speaker_name?: string;
      clear_speaker?: boolean;
    }) => meetingsApi.assignSpeakerByLabel(workspaceId, recordingId, input),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: meetingKeys.detail(workspaceId, recordingId),
      }),
  });
}

const speakerNameKeys = (workspaceId: string, recordingId: string) =>
  ["meetings", workspaceId, recordingId, "speaker-names"] as const;

/** The newest name search; polls while the model is reading the transcript. */
export function useSpeakerNameRun(
  workspaceId: string,
  recordingId: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: speakerNameKeys(workspaceId, recordingId),
    queryFn: () => meetingsApi.getSpeakerNameRun(workspaceId, recordingId),
    enabled: enabled && Boolean(workspaceId) && Boolean(recordingId),
    refetchInterval: (query) => {
      const status = (query.state.data as SpeakerNameRun | null | undefined)
        ?.status;
      return status === "pending" || status === "processing" ? 2000 : false;
    },
  });
}

export function useIdentifySpeakerNames(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      meetingsApi.identifySpeakerNames(workspaceId, recordingId),
    onSuccess: (run) =>
      qc.setQueryData(speakerNameKeys(workspaceId, recordingId), run),
  });
}

/** Use or dismiss one suggested name; using it renames the speaker. */
export function useDecideSpeakerName(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      runId: string;
      index: number;
      decision: "accept" | "dismiss";
    }) =>
      meetingsApi.decideSpeakerName(
        workspaceId,
        recordingId,
        input.runId,
        input.index,
        input.decision,
      ),
    onSuccess: (run, input) => {
      qc.setQueryData(speakerNameKeys(workspaceId, recordingId), run);
      if (input.decision === "accept")
        void qc.invalidateQueries({
          queryKey: meetingKeys.detail(workspaceId, recordingId),
        });
    },
  });
}

/** Pending uncertain speaker matches for a finished recording (issue #6). */
export function useSpeakerSuggestions(
  workspaceId: string,
  recordingId: string | null,
  enabled = true,
) {
  return useQuery({
    queryKey: [
      ...meetingKeys.detail(workspaceId, recordingId ?? ""),
      "suggestions",
    ],
    queryFn: () =>
      meetingsApi.listSpeakerSuggestions(workspaceId, recordingId!),
    enabled: enabled && Boolean(workspaceId) && Boolean(recordingId),
  });
}

export function useAcceptSpeakerSuggestion(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (suggestionId: string) =>
      meetingsApi.acceptSpeakerSuggestion(
        workspaceId,
        recordingId,
        suggestionId,
      ),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: meetingKeys.detail(workspaceId, recordingId),
      }),
  });
}

export function useRejectSpeakerSuggestion(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (suggestionId: string) =>
      meetingsApi.rejectSpeakerSuggestion(
        workspaceId,
        recordingId,
        suggestionId,
      ),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: meetingKeys.detail(workspaceId, recordingId),
      }),
  });
}

/** A recording's transcript revisions, newest first (issue #6, C2). */
export function useRevisions(
  workspaceId: string,
  recordingId: string | null,
  enabled = true,
) {
  return useQuery({
    queryKey: [
      ...meetingKeys.detail(workspaceId, recordingId ?? ""),
      "revisions",
    ],
    queryFn: () => meetingsApi.listRevisions(workspaceId, recordingId!),
    enabled: enabled && Boolean(workspaceId) && Boolean(recordingId),
  });
}

/** One revision's segments (compare/review reads). */
export function useRevisionSegments(
  workspaceId: string,
  recordingId: string,
  revisionId: string | null,
) {
  return useQuery({
    queryKey: [
      ...meetingKeys.detail(workspaceId, recordingId),
      "revisions",
      revisionId ?? "",
      "segments",
    ],
    queryFn: () =>
      meetingsApi.getRevisionSegments(workspaceId, recordingId, revisionId!),
    enabled: Boolean(revisionId),
  });
}

export function useApproveRevision(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { revisionId: string; note?: string }) =>
      meetingsApi.approveRevision(
        workspaceId,
        recordingId,
        input.revisionId,
        input.note,
      ),
    onSettled: () =>
      qc.invalidateQueries({
        queryKey: meetingKeys.detail(workspaceId, recordingId),
      }),
  });
}

export function useDiscardRevision(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (revisionId: string) =>
      meetingsApi.discardRevision(workspaceId, recordingId, revisionId),
    onSettled: () =>
      qc.invalidateQueries({
        queryKey: meetingKeys.detail(workspaceId, recordingId),
      }),
  });
}

/** Server-computed audio capabilities — the UI shows/hides controls from
 * THIS, never from client-side role guesses (issue #6, E1). */
export function useMeetingPermissions(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["meetings", workspaceId, "permissions"],
    queryFn: async () =>
      new Set(
        (await meetingsApi.getMeetingPermissions(workspaceId)).capabilities,
      ),
    enabled: enabled && Boolean(workspaceId),
    staleTime: 60_000,
  });
}

const commentKeys = (workspaceId: string, recordingId: string) =>
  ["meetings", workspaceId, recordingId, "comments"] as const;

/** A recording's comment threads (issue #6, D4). */
export function useMeetingComments(
  workspaceId: string,
  recordingId: string,
  includeResolved: boolean,
) {
  return useQuery({
    queryKey: [...commentKeys(workspaceId, recordingId), includeResolved],
    queryFn: () =>
      meetingsApi.listMeetingComments(
        workspaceId,
        recordingId,
        includeResolved,
      ),
    enabled: Boolean(workspaceId) && Boolean(recordingId),
  });
}

export function useAddMeetingComment(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      body: string;
      parent_id?: string;
      at_ms?: number;
      segment_id?: string;
      anchor_quote?: string;
      mentions?: string[];
    }) => meetingsApi.addMeetingComment(workspaceId, recordingId, input),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: commentKeys(workspaceId, recordingId) }),
  });
}

export function useUpdateMeetingComment(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { commentId: string; body: string }) =>
      meetingsApi.updateMeetingComment(
        workspaceId,
        recordingId,
        input.commentId,
        input.body,
      ),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: commentKeys(workspaceId, recordingId) }),
  });
}

export function useDeleteMeetingComment(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (commentId: string) =>
      meetingsApi.deleteMeetingComment(workspaceId, recordingId, commentId),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: commentKeys(workspaceId, recordingId) }),
  });
}

export function useResolveMeetingComment(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { commentId: string; resolved: boolean }) =>
      meetingsApi.resolveMeetingComment(
        workspaceId,
        recordingId,
        input.commentId,
        input.resolved,
      ),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: commentKeys(workspaceId, recordingId) }),
  });
}

const clipKeys = (workspaceId: string, recordingId: string) =>
  ["meetings", workspaceId, recordingId, "clips"] as const;

/** A recording's clips; polls while one is being sliced (issue #6, D5). */
export function useClips(workspaceId: string, recordingId: string) {
  return useQuery({
    queryKey: clipKeys(workspaceId, recordingId),
    queryFn: () => meetingsApi.listClips(workspaceId, recordingId),
    enabled: Boolean(workspaceId) && Boolean(recordingId),
    refetchInterval: (query) =>
      (query.state.data as MeetingClip[] | undefined)?.some(
        (c) => c.status === "pending" || c.status === "processing",
      )
        ? 2000
        : false,
  });
}

export function useCreateClip(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { title: string; start_ms: number; end_ms: number }) =>
      meetingsApi.createClip(workspaceId, recordingId, input),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: clipKeys(workspaceId, recordingId) }),
  });
}

export function useDeleteClip(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (clipId: string) =>
      meetingsApi.deleteClip(workspaceId, recordingId, clipId),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: clipKeys(workspaceId, recordingId) }),
  });
}

const dlpKeys = (workspaceId: string, recordingId: string) =>
  ["meetings", workspaceId, recordingId, "dlp"] as const;

/** The display revision's DLP findings (issue #6, E4). */
export function useDlpFindings(
  workspaceId: string,
  recordingId: string,
  enabled: boolean,
  /** Re-read every 1.5 s while a requested scan is being worked on. */
  polling = false,
) {
  return useQuery({
    queryKey: dlpKeys(workspaceId, recordingId),
    queryFn: () => meetingsApi.listDlpFindings(workspaceId, recordingId),
    enabled: enabled && Boolean(workspaceId) && Boolean(recordingId),
    refetchInterval: polling ? 1500 : false,
  });
}

const dlpTermKeys = (workspaceId: string) =>
  ["meetings", workspaceId, "dlp-terms"] as const;

/** The workspace's own words and patterns to look for. */
export function useDlpTerms(workspaceId: string) {
  return useQuery({
    queryKey: dlpTermKeys(workspaceId),
    queryFn: () => meetingsApi.listDlpTerms(workspaceId),
    enabled: Boolean(workspaceId),
  });
}

export function useAddDlpTerm(workspaceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      pattern: string;
      is_regex: boolean;
      label: string | null;
    }) => meetingsApi.addDlpTerm(workspaceId, input),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: dlpTermKeys(workspaceId) }),
  });
}

export function useDeleteDlpTerm(workspaceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (termId: string) =>
      meetingsApi.deleteDlpTerm(workspaceId, termId),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: dlpTermKeys(workspaceId) }),
  });
}

export function useSetDlpFindingStatus(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      findingId: string;
      status: "pending" | "accepted" | "dismissed";
    }) =>
      meetingsApi.setDlpFindingStatus(
        workspaceId,
        recordingId,
        input.findingId,
        input.status,
      ),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: dlpKeys(workspaceId, recordingId) }),
  });
}

export function useAddManualDlpFinding(
  workspaceId: string,
  recordingId: string,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      segment_id: string;
      start_char: number;
      end_char: number;
    }) => meetingsApi.addManualDlpFinding(workspaceId, recordingId, input),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: dlpKeys(workspaceId, recordingId) }),
  });
}

export function useRequestDlpScan(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => meetingsApi.requestDlpScan(workspaceId, recordingId),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: dlpKeys(workspaceId, recordingId) }),
  });
}

/** Apply accepted findings: the worker swaps in a masked revision + muted
 * audio, so the whole recording query tree refreshes afterwards. */
export function useApplyRedaction(workspaceId: string, recordingId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => meetingsApi.applyRedaction(workspaceId, recordingId),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: ["meetings", workspaceId] }),
  });
}
