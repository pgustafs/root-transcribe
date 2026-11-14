/** System/health and public app config API. */

import { apiClient } from "@/api/client";

/** Public, non-sensitive settings sourced from the backend (e.g. the app name). */
export type AppConfig = {
  keycloak_url: string;
  keycloak_realm: string;
  keycloak_client_id: string;
  project_name: string;
  meetings_enabled: boolean;
  /** Live transcription-path signal: available | degraded | unavailable (#6). */
  meetings_status: "available" | "degraded" | "unavailable";
  /** Whether recording requires the participant-consent attestation (#6). */
  meeting_consent_required: boolean;
  audio_max_bytes: number;
  recording_max_minutes: number;
};

export async function getConfig(signal?: AbortSignal): Promise<AppConfig> {
  const { data } = await apiClient.get<AppConfig>("/config", { signal });
  return data;
}
