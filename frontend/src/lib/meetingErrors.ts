/**
 * Safe, localized presentation of a meeting recording's failure (issue #37).
 *
 * The backend classifies every transcription failure into a stable `error_code`
 * and stores a safe message. The UI must NEVER render the raw `error` string
 * verbatim — legacy rows can carry ffmpeg commands / temp paths — so we map the
 * code to localized copy and fall back to a generic safe message otherwise.
 *
 * Mirrors `app/services/media_validation.py::MediaErrorCode` and its
 * non-retryable set; keep the two in sync.
 */

import axios from "axios";
import type { TFunction } from "i18next";

export const MEETING_ERROR_CODES = [
  "recording_invalid_container",
  "recording_incomplete",
  "recording_unsupported_codec",
  "recording_limit_exceeded",
  "recording_decode_timeout",
  "transcription_service_unavailable",
  "transcription_internal_error",
] as const;

export type MeetingErrorCode = (typeof MEETING_ERROR_CODES)[number];

/** Retrying the SAME stored object fails identically for these — a new/recovered
 * recording is the only fix (mirrors the backend `/retry` gate). */
const NON_RETRYABLE = new Set<string>([
  "recording_invalid_container",
  "recording_incomplete",
  "recording_unsupported_codec",
  "recording_limit_exceeded",
]);

function isKnownCode(code: string | null): code is MeetingErrorCode {
  return (
    code != null && (MEETING_ERROR_CODES as readonly string[]).includes(code)
  );
}

/**
 * Whether a failed recording with this `error_code` can usefully be retried.
 * Legacy/unknown (`null`) is treated as retryable — we only suppress retry when
 * we positively know it's a non-retryable media fault (matches the backend).
 */
export function isRetryableMeetingError(code: string | null): boolean {
  if (!code) return true;
  return !NON_RETRYABLE.has(code);
}

/**
 * A SAFE, localized failure message for display. A known code maps to localized
 * copy; anything else (legacy raw `error`, unknown code) falls back to a generic
 * safe message — the raw string is never shown, so no command/path can leak.
 */
export function meetingErrorMessage(
  errorCode: string | null,
  t: TFunction,
): string {
  if (isKnownCode(errorCode)) {
    return t(`meetings:detail.errorCodes.${errorCode}`);
  }
  return t("meetings:detail.errorGeneric");
}

/** Localized approve/discard errors, based on the stable HTTP status. */
export function revisionActionErrorMessage(
  error: unknown,
  t: TFunction,
): string {
  const status = axios.isAxiosError(error) ? error.response?.status : undefined;
  if (status === 401 || status === 403)
    return t("meetings:detail.revisionErrors.forbidden");
  if (status === 404) return t("meetings:detail.revisionErrors.notFound");
  if (status === 409) return t("meetings:detail.revisionErrors.conflict");
  if (status === undefined && axios.isAxiosError(error))
    return t("meetings:detail.revisionErrors.unavailable");
  return t("meetings:detail.revisionErrors.generic");
}
