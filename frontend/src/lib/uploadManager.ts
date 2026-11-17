/**
 * Resumable meeting upload (issue #6, wave B4).
 *
 * Drives the wave-B1 upload-session API: open a session, PUT bounded parts
 * with SHA-256 checksums (a mismatch is a retryable 422 — the part is simply
 * re-sent), complete with a manifest of every part's checksum. Parts already
 * accepted by the server are skipped on resume, so a reload mid-upload
 * continues instead of restarting, and an abort truly cancels (DELETE discards
 * the server-side parts). Progress is reported per part for the recording bar.
 */

import {
  completeUploadSession,
  createUploadSession,
  tryAbortUploadSession,
  uploadSessionPart,
} from "@/api/meetings";
import type { MeetingClientDiagnostics } from "@/api/meetings";
import type {
  AudioLayout,
  MeetingRecording,
  RecordingLanguage,
} from "@/api/types";

/** Thrown when the user cancels an upload.
 *
 * `serverConfirmed` is the ONLY safe basis for discarding the local recording:
 * the server refuses cancellation while a completion holds a fresh lease, so an
 * unconfirmed cancel means the upload may still complete (issue #37 r13).
 */
export class UploadCancelledError extends Error {
  readonly name = "AbortError";

  constructor(readonly serverConfirmed: boolean) {
    super("Upload cancelled");
  }
}

export function isUploadCancelled(
  error: unknown,
): error is UploadCancelledError {
  return error instanceof UploadCancelledError;
}

export type UploadProgress = {
  partsDone: number;
  partsTotal: number;
  sentBytes: number;
  totalBytes: number;
};

export type UploadSessionInfo = {
  uploadSessionId: string;
  recordingId: string;
  partSizeBytes: number;
};

export type ResumeInfo = UploadSessionInfo & {
  /** part number -> sha256 already accepted by the server. */
  uploadedParts: Record<number, string>;
};

export async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function uploadRecording(options: {
  workspaceId: string;
  blob: Blob;
  filename: string;
  title: string;
  consent: boolean;
  requestedLanguage?: RecordingLanguage;
  expectedSpeakers?: number | null;
  /** What the channels hold (an online meeting: microphone left, tab right). */
  audioLayout?: AudioLayout;
  micSpeakers?: number | null;
  signal?: AbortSignal;
  resume?: ResumeInfo;
  /**
   * Fired once the session exists — persist it so a reload can resume. AWAITED
   * before any part is sent (issue #37 review): if persistence were
   * fire-and-forget, a crash right after the server created the session would
   * lose its ids and the next attempt would open a SECOND server upload.
   */
  onSession?: (info: UploadSessionInfo) => void | Promise<void>;
  /** Fired after each accepted part — persist for resume bookkeeping. Awaited
   * so the resume manifest on disk never lags the server's accepted parts. */
  onPartDone?: (partNumber: number, sha256: string) => void | Promise<void>;
  onProgress?: (progress: UploadProgress) => void;
  /** Privacy-safe recorder context stored with the recording (issue #37 r7). */
  clientDiagnostics?: MeetingClientDiagnostics;
}): Promise<MeetingRecording> {
  const { workspaceId, blob, signal } = options;

  let session: UploadSessionInfo;
  const uploaded: Record<number, string> = { ...options.resume?.uploadedParts };
  if (options.resume) {
    session = options.resume;
  } else {
    const created = await createUploadSession(workspaceId, {
      title: options.title,
      filename: options.filename,
      size_bytes: blob.size,
      participant_consent: options.consent,
      requested_language: options.requestedLanguage ?? "auto",
      expected_speakers: options.expectedSpeakers ?? null,
      audio_layout: options.audioLayout ?? "mixed",
      mic_speakers: options.micSpeakers ?? null,
    });
    session = {
      uploadSessionId: created.id,
      recordingId: created.recording_id,
      partSizeBytes: created.part_size_bytes,
    };
    // Awaited: the ids must be durable BEFORE we start sending parts.
    await options.onSession?.(session);
  }

  const partSize = session.partSizeBytes;
  const partsTotal = Math.max(1, Math.ceil(blob.size / partSize));
  const report = (partsDone: number) =>
    options.onProgress?.({
      partsDone,
      partsTotal,
      sentBytes: Math.min(partsDone * partSize, blob.size),
      totalBytes: blob.size,
    });

  /** Cancel server-side and throw an AbortError that says whether the server
   * CONFIRMED it. The caller needs that distinction: a refused cancel (409 —
   * a completion holds a fresh lease) means the upload may still finish, so
   * the local copy must be kept (issue #37 review round 13). */
  const cancel = async (): Promise<never> => {
    const confirmed = await tryAbortUploadSession(
      workspaceId,
      session.uploadSessionId,
    );
    throw new UploadCancelledError(confirmed);
  };

  /** Run one request so that a cancellation NEVER escapes as a plain transport
   * error. Every call the upload makes goes through here: handling the abort at
   * individual call sites meant the checksum retry — nested inside the first
   * attempt's catch — had no handler at all, so cancelling while it was pending
   * surfaced as an ordinary CanceledError and the server was never told to
   * discard the session (issue #37 review round 15). */
  const guarded = async <T>(request: () => Promise<T>): Promise<T> => {
    try {
      return await request();
    } catch (error) {
      if (signal?.aborted) {
        await cancel(); // throws UploadCancelledError
      }
      throw error;
    }
  };

  let partsDone = Object.keys(uploaded).length;
  report(partsDone);
  const manifest: { part_number: number; sha256: string }[] = [];
  for (let partNumber = 1; partNumber <= partsTotal; partNumber += 1) {
    if (signal?.aborted) {
      await cancel();
    }
    const start = (partNumber - 1) * partSize;
    const slice = blob.slice(start, Math.min(start + partSize, blob.size));
    const buffer = await slice.arrayBuffer();
    const digest = await sha256Hex(buffer);
    manifest.push({ part_number: partNumber, sha256: digest });
    if (uploaded[partNumber] === digest) {
      continue; // already accepted by the server (resume)
    }
    // One automatic retry on a checksum 422 (corruption in flight). BOTH
    // attempts carry the signal and both are `guarded`: a part PUT can stall
    // indefinitely, and a cancel that cannot interrupt it left the upload
    // running with the server never told to discard it (issue #37 r14/r15).
    const putPart = () =>
      guarded(() =>
        uploadSessionPart(
          workspaceId,
          session.uploadSessionId,
          partNumber,
          buffer,
          digest,
          signal,
        ),
      );
    try {
      await putPart();
    } catch (error) {
      if (isChecksumMismatch(error)) {
        await putPart();
      } else {
        throw error;
      }
    }
    uploaded[partNumber] = digest;
    await options.onPartDone?.(partNumber, digest);
    partsDone += 1;
    report(partsDone);
  }

  if (signal?.aborted) {
    await cancel();
  }
  return completeWithRetry(options, session.uploadSessionId, manifest, cancel);
}

/** A sleep that gives up the moment the caller cancels.
 *
 * A bare `setTimeout` made cancel take up to a full retry interval to be
 * noticed — and the loop then issued ANOTHER completion request for an upload
 * the user had already cancelled (issue #37 review round 12). */
function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  if (!signal) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      resolve();
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** How long the server's completion lease runs, and how patiently we wait it
 * out. 425 means another caller is finishing RIGHT NOW; retrying past the lease
 * lets us reclaim if that caller died (issue #37 review round 11). */
const COMPLETION_RETRY_MS = 15_000;
const COMPLETION_MAX_ATTEMPTS = 12; // ~3 min, comfortably past the 2 min lease

function isCompletionInProgress(error: unknown): boolean {
  return (
    (error as { response?: { status?: number } })?.response?.status === 425
  );
}

/**
 * Complete the upload, waiting out a completion that another caller is already
 * running. Answering that case with a success made the client stop calling
 * completion altogether — so a holder that then died stranded the recording
 * with nobody to reclaim it. Here we keep asking until it finishes or the lease
 * expires and this attempt takes over.
 */
async function completeWithRetry(
  options: {
    workspaceId: string;
    signal?: AbortSignal;
    clientDiagnostics?: MeetingClientDiagnostics;
  },
  uploadSessionId: string,
  manifest: { part_number: number; sha256: string }[],
  onCancel: () => Promise<never>,
): Promise<MeetingRecording> {
  for (let attempt = 1; ; attempt += 1) {
    // Checked at LOOP ENTRY, not only after a failure: a cancel arriving during
    // the wait must stop us BEFORE the next request goes out. Checking it only
    // in the catch meant the delay ran to completion and one more completion
    // was issued for an upload the user had cancelled (r12).
    if (options.signal?.aborted) {
      await onCancel();
    }
    try {
      return await completeUploadSession(
        options.workspaceId,
        uploadSessionId,
        manifest,
        options.clientDiagnostics,
        // The signal reaches the REQUEST, so a cancel during a long-pending
        // completion aborts it instead of letting its 202 be accepted after
        // the user cancelled (issue #37 review round 13).
        options.signal,
      );
    } catch (error) {
      // A cancel that killed the in-flight POST lands here as a transport
      // abort, not a 425 — go straight to cleanup rather than treating it as
      // an ordinary failure.
      if (options.signal?.aborted) {
        await onCancel(); // throws
      }
      if (
        !isCompletionInProgress(error) ||
        attempt >= COMPLETION_MAX_ATTEMPTS
      ) {
        throw error;
      }
      // Abortable: cancel is noticed immediately instead of up to a full
      // retry interval later. The loop-entry check above then does the
      // cleanup — the server either cancels the session or, if a completion
      // holds a fresh lease, refuses and lets that completion finish.
      await abortableDelay(COMPLETION_RETRY_MS, options.signal);
    }
  }
}

function isChecksumMismatch(error: unknown): boolean {
  const status = (error as { response?: { status?: number } })?.response
    ?.status;
  return status === 422;
}
