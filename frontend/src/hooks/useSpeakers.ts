/** React Query hooks for the speaker directory. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as speakersApi from "@/api/speakers";

export const speakerKeys = {
  list: ["speakers"] as const,
  mine: ["speakers", "me"] as const,
};

/** The directory (names + counts) — used by admin + the transcript typeahead. */
export function useSpeakers(enabled = true) {
  return useQuery({
    queryKey: speakerKeys.list,
    queryFn: speakersApi.listSpeakers,
    enabled,
  });
}

export function useMySpeaker(enabled = true) {
  return useQuery({
    queryKey: speakerKeys.mine,
    queryFn: speakersApi.getMySpeaker,
    enabled,
  });
}

export function useCreateSpeaker() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: speakersApi.createSpeaker,
    onSuccess: () => qc.invalidateQueries({ queryKey: speakerKeys.list }),
  });
}

export function useUpdateSpeaker() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      id: string;
      display_name?: string;
      status?: "active" | "revoked";
    }) =>
      speakersApi.updateSpeaker(input.id, {
        display_name: input.display_name,
        status: input.status,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: speakerKeys.list }),
  });
}

export function useDeleteSpeaker() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => speakersApi.deleteSpeaker(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: speakerKeys.list }),
  });
}

export function useEnrollVoiceprint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { id: string; file: Blob; filename: string }) =>
      speakersApi.enrollVoiceprint(input.id, input.file, input.filename),
    onSuccess: () => qc.invalidateQueries({ queryKey: speakerKeys.list }),
  });
}

export function useEnrollMyVoiceprint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { file: Blob; filename: string; consent: boolean }) =>
      speakersApi.enrollMyVoiceprint(input.file, input.filename, input.consent),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: speakerKeys.mine });
      qc.invalidateQueries({ queryKey: speakerKeys.list });
    },
  });
}

export function useDeleteMyVoiceprints() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: speakersApi.deleteMyVoiceprints,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: speakerKeys.mine });
      qc.invalidateQueries({ queryKey: speakerKeys.list });
    },
  });
}
