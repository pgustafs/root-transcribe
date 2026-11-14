import { describe, expect, it } from "vitest";

import {
  MEETING_ERROR_CODES,
  isRetryableMeetingError,
  meetingErrorMessage,
} from "./meetingErrors";

// A minimal t() stand-in: echoes the key so we can assert which key was chosen
// without pulling in i18next.
const t = ((key: string) => key) as unknown as Parameters<
  typeof meetingErrorMessage
>[1];

describe("isRetryableMeetingError", () => {
  it("treats the four media faults as non-retryable", () => {
    for (const code of [
      "recording_invalid_container",
      "recording_incomplete",
      "recording_unsupported_codec",
      "recording_limit_exceeded",
    ]) {
      expect(isRetryableMeetingError(code)).toBe(false);
    }
  });

  it("treats infra/internal/timeout codes as retryable", () => {
    for (const code of [
      "recording_decode_timeout",
      "transcription_service_unavailable",
      "transcription_internal_error",
    ]) {
      expect(isRetryableMeetingError(code)).toBe(true);
    }
  });

  it("treats legacy null / unknown codes as retryable", () => {
    expect(isRetryableMeetingError(null)).toBe(true);
    expect(isRetryableMeetingError("something_new")).toBe(true);
  });
});

describe("meetingErrorMessage", () => {
  it("maps every known code to its own localized key", () => {
    for (const code of MEETING_ERROR_CODES) {
      expect(meetingErrorMessage(code, t)).toBe(
        `meetings:detail.errorCodes.${code}`,
      );
    }
  });

  it("falls back to the generic key for null / unknown codes (never the raw error)", () => {
    expect(meetingErrorMessage(null, t)).toBe("meetings:detail.errorGeneric");
    expect(meetingErrorMessage("bogus_code", t)).toBe(
      "meetings:detail.errorGeneric",
    );
  });
});
