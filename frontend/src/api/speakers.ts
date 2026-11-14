/** Speaker directory API: enrolled people + voiceprints for identification. */

import { apiClient } from "@/api/client";
import type { SpeakerProfile } from "@/api/types";

export async function listSpeakers(): Promise<SpeakerProfile[]> {
  const { data } = await apiClient.get<SpeakerProfile[]>("/speakers");
  return data;
}

export async function getMySpeaker(): Promise<SpeakerProfile | null> {
  const { data } = await apiClient.get<SpeakerProfile | null>("/speakers/me");
  return data;
}

export async function createSpeaker(input: {
  display_name: string;
  user_id?: string | null;
  consent_granted: boolean;
  consent_evidence?: string;
}): Promise<SpeakerProfile> {
  const { data } = await apiClient.post<SpeakerProfile>("/speakers", input);
  return data;
}

export async function updateSpeaker(
  id: string,
  patch: { display_name?: string; status?: "active" | "revoked" },
): Promise<SpeakerProfile> {
  const { data } = await apiClient.patch<SpeakerProfile>(
    `/speakers/${id}`,
    patch,
  );
  return data;
}

export async function deleteSpeaker(id: string): Promise<void> {
  await apiClient.delete(`/speakers/${id}`);
}

export async function enrollVoiceprint(
  id: string,
  file: Blob,
  filename: string,
): Promise<SpeakerProfile> {
  const form = new FormData();
  form.append("file", file, filename);
  const { data } = await apiClient.post<SpeakerProfile>(
    `/speakers/${id}/voiceprints`,
    form,
    { headers: { "Content-Type": undefined } },
  );
  return data;
}

export async function enrollMyVoiceprint(
  file: Blob,
  filename: string,
  consent: boolean,
): Promise<SpeakerProfile> {
  const form = new FormData();
  form.append("file", file, filename);
  form.append("consent_granted", String(consent));
  const { data } = await apiClient.post<SpeakerProfile>(
    "/speakers/me/voiceprints",
    form,
    { headers: { "Content-Type": undefined } },
  );
  return data;
}

export async function deleteMyVoiceprints(): Promise<void> {
  await apiClient.delete("/speakers/me/voiceprints");
}

/** One DSAR bundle build (issue #6, E5; admin only). */
export type ComplianceExport = {
  id: string;
  subject_email: string;
  speaker_profile_id: string | null;
  status: "pending" | "running" | "completed" | "failed";
  size_bytes: number | null;
  error: string | null;
  requested_by_email: string | null;
  created_at: string;
  updated_at: string;
};

export async function listComplianceExports(): Promise<ComplianceExport[]> {
  const { data } = await apiClient.get<ComplianceExport[]>(
    "/speakers/compliance-exports",
  );
  return data;
}

export async function requestComplianceExport(
  subjectEmail: string,
): Promise<ComplianceExport> {
  const { data } = await apiClient.post<ComplianceExport>(
    "/speakers/compliance-exports",
    { subject_email: subjectEmail },
  );
  return data;
}

export async function downloadComplianceExport(
  jobId: string,
): Promise<{ blob: Blob; filename: string | null }> {
  const response = await apiClient.get(
    `/speakers/compliance-exports/${jobId}/download`,
    { responseType: "blob" },
  );
  const disposition = (response.headers["content-disposition"] ?? "") as string;
  const match = /filename="([^"]+)"/.exec(disposition);
  return { blob: response.data as Blob, filename: match?.[1] ?? null };
}
