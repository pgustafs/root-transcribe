import { createPortal } from "react-dom";
import type { RecordingLanguage } from "@/api/types";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";

import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import {
  abortUploadSession,
  getRecording,
  type MeetingClientEventName,
  reportClientEvent,
  tryAbortUploadSession,
} from "@/api/meetings";
import { Alert } from "@/components/Alert";
import { RecordingDock } from "@/components/recording/RecordingDock";
import { meetingKeys } from "@/hooks/useMeetings";
import { useMediaRecorder } from "@/hooks/useMediaRecorder";
import { useNavigate } from "react-router-dom";
import { ChangeSourceDialog } from "@/components/study/ChangeSourceDialog";
import { useFloatingWindow } from "@/hooks/useFloatingWindow";
import { FloatingNotePanel } from "@/components/study/FloatingNotePanel";
import type { Crop } from "@/lib/study/frame";
import {
  discardStudySessions,
  IDLE_STUDY,
  linkStudySessions,
  recoverStudyNotes,
  StudyNoteController,
  type StudyStatus,
} from "@/lib/study/noteController";
import type { StudyMode } from "@/lib/study/noteStore";
import {
  appendChunk,
  assembleRecording,
  assemblyDiagnostics,
  beginLiveCapture,
  createStoredSession,
  RecordingStoreBlockedError,
  RecordingStoreOutdatedError,
  RecordingStoreUnavailableError,
  setStorageFailureListener,
  noteExpectedChunk,
  deleteStoredSession,
  getStoredSession,
  listStoredSessions,
  purgeExpiredSessions,
  recordUploadedPart,
  type StoredRecordingSession,
  updateStoredSession,
} from "@/lib/recordingStore";
import {
  BROWSER_FAMILIES,
  type BrowserInfo,
  browserInfo,
  mediaTokens,
} from "@/lib/clientDiagnostics";
import { meetingErrorMessage } from "@/lib/meetingErrors";
import {
  type PreflightReason,
  preflightRecording,
} from "@/lib/recordingPreflight";
import {
  isUploadCancelled,
  type UploadProgress,
  uploadRecording,
} from "@/lib/uploadManager";

export type StartRecordingOptions = {
  workspaceId: string;
  title: string;
  includeTabAudio: boolean;
  consent: boolean;
  requestedLanguage?: RecordingLanguage;
  /** Specific input device (from listAudioInputs), default mic otherwise. */
  deviceId?: string;
  /** Called with the created recording id once the upload is accepted. */
  onUploaded?: (recordingId: string) => void;
  /** Take study notes from the shared tab into this notebook. */
  study?: StudyTarget;
};

export type StudyTarget = {
  notebookId: string;
  notebookTitle: string;
  mode: StudyMode;
};

type RecordingContextValue = {
  /** Workspace currently being recorded into (null = idle). */
  activeWorkspaceId: string | null;
  isSupported: boolean;
  /** Begin recording; resolves true when live (false: mic denied / unsupported). */
  start: (opts: StartRecordingOptions) => Promise<boolean>;
  /** Last recorder/start error ("insecure-context" is a sentinel). */
  error: string | null;
  /** A localized reason the start could not even begin (storage locked by
   * another tab, etc.) — NOT a microphone failure. */
  startError: string | null;
  /** Study notes of the running recording (inactive when none are taken). */
  study: StudyStatus;
  /** A still of the whole shared frame, for choosing the video area. */
  studyPreview: () => Promise<Blob>;
  studyCrop: () => Crop;
  setStudyCrop: (crop: Crop) => Promise<void>;
  /** Open the floating Take note window (call from a click). */
  openStudyFloat: () => void;
};

const RecordingContext = createContext<RecordingContextValue | null>(null);

export function useRecordingContext(): RecordingContextValue {
  const ctx = useContext(RecordingContext);
  if (!ctx) throw new Error("useRecordingContext outside RecordingProvider");
  return ctx;
}

/** How often, for how long, and with what per-request ceiling we ask the server
 * whether an uploaded recording passed media validation (issue #37). The
 * deadline is ABSOLUTE per session, so it cannot be reset by re-renders; when it
 * expires we stop polling but KEEP the local copy, and the next mount re-checks. */
const VALIDATION_POLL_MS = 10_000;
const MAX_VALIDATION_MS = 10 * 60_000; // 10 minutes per session
const VALIDATION_REQUEST_TIMEOUT_MS = 8_000;

/** Localized message key per client-preflight reason (issue #37 review: the
 * reason must not be discarded — "empty" and "wrong container" are different
 * user problems). */
const PREFLIGHT_MESSAGE_KEYS: Record<PreflightReason, string> = {
  empty: "recordingBar.mediaEmpty",
  too_small: "recordingBar.mediaTooSmall",
  bad_header: "recordingBar.mediaInvalid",
};

/** Reason -> the stable category operators see (issue #37 observability). */
export const PREFLIGHT_EVENTS: Record<PreflightReason, MeetingClientEventName> =
  {
    empty: "preflight_empty",
    too_small: "preflight_too_small",
    bad_header: "preflight_bad_header",
  };

/** The browser that produced a stored recording, falling back to the current one
 * for legacy sessions recorded before this was captured (issue #37 r6). */
function producerBrowser(session: StoredRecordingSession): BrowserInfo {
  const stored = session.clientFamily;
  if (stored && (BROWSER_FAMILIES as readonly string[]).includes(stored)) {
    return {
      family: stored as BrowserInfo["family"],
      major: session.clientMajor ?? null,
    };
  }
  // Legacy sessions (recorded before this was captured) fall back to the
  // current browser rather than dropping the diagnostic entirely.
  return browserInfo();
}

/**
 * The server says this upload session can't be used: 409 (already completed,
 * manifest/checksum mismatch, no longer active) or 404 (expired, aborted, or
 * pruned). On its own it proves NOTHING about whether the recording made it —
 * the caller must ask the recording to tell "already finished" from "genuinely
 * dead" (issue #37 review rounds 6-7).
 */
function isUploadSessionUnusable(error: unknown): boolean {
  const status = (error as { response?: { status?: number } })?.response
    ?.status;
  return status === 409 || status === 404;
}

/** A DEFINITE "it isn't there", as opposed to a transient failure. Only this
 * may retire stored ids; anything else leaves the outcome unknown. */
function isNotFound(error: unknown): boolean {
  return (
    (error as { response?: { status?: number } })?.response?.status === 404
  );
}

const extFromMime = (mimeType: string): string =>
  mimeType.includes("ogg") ? "ogg" : mimeType.includes("mp4") ? "mp4" : "webm";

export function RecordingProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation(["meetings", "common", "study"]);
  const recorder = useMediaRecorder();
  const navigate = useNavigate();
  // Study notes: one controller for the dock button and the floating window.
  const [study, setStudy] = useState<StudyStatus>(IDLE_STUDY);
  const recorderRef = useRef(recorder);
  recorderRef.current = recorder;
  const studyRef = useRef<StudyNoteController | null>(null);
  if (studyRef.current === null) {
    studyRef.current = new StudyNoteController({
      clock: () => recorderRef.current.getElapsedMs(),
      isPaused: () => recorderRef.current.isPaused(),
      onChange: setStudy,
    });
  }
  const studyController = studyRef.current;
  const floating = useFloatingWindow({
    title: t("study:dock.takeNote"),
    width: 280,
    height: 132,
  });
  const closeFloating = floating.close;
  const openFloating = floating.open;
  useEffect(() => {
    // No notes can be taken any more: the floating button goes away.
    if (!study.active) closeFloating();
  }, [study.active, closeFloating]);
  const [changingSource, setChangingSource] = useState(false);
  useEffect(() => {
    // Notes left behind by a reload or a crash are finished and sent.
    void recoverStudyNotes();
    return () => {
      void studyController.end("interrupted");
    };
  }, [studyController]);
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<StartRecordingOptions | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  // Stopped-but-unuploaded local session (retry keeps working from IDB).
  const [pendingLocalId, setPendingLocalId] = useState<string | null>(null);
  // Cancel is DESTRUCTIVE (it discards captured audio), so the dock's Cancel
  // only opens this confirmation; nothing is discarded on a single click.
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  /** Why a start attempt failed before the recorder ever ran (e.g. the local
   * database is locked by another tab). Surfaced by the Record dialog. */
  const [startError, setStartError] = useState<string | null>(null);
  /** The durable copy has stopped updating mid-capture. Writes never throw
   * into the recorder, so this is how the loss becomes visible instead of
   * silent (issue #37 review round 20). */
  const [storageDegraded, setStorageDegraded] = useState(false);
  const releaseCaptureRef = useRef<(() => void) | null>(null);
  // Recordings found in IndexedDB on mount — a previous tab/session that
  // crashed or reloaded. Offered as resume/discard until acted on.
  const [recoverable, setRecoverable] = useState<StoredRecordingSession[]>([]);
  // Uploaded, awaiting the worker's media verdict — local copy kept (issue #37).
  const [awaitingValidation, setAwaitingValidation] = useState<
    StoredRecordingSession[]
  >([]);
  // The worker REJECTED the media: the local copy is deliberately retained so
  // the meeting isn't lost, and the user gets the safe localized reason.
  const [rejected, setRejected] = useState<
    { session: StoredRecordingSession; code: string | null }[]
  >([]);
  const localIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Highest chunk sequence the recorder emitted + 1 — the authoritative count
  // persisted at stop, complementing the per-chunk intent records (issue #37).
  const emittedChunksRef = useRef(0);
  // sessionId -> absolute deadline for the validation watch (issue #37 review:
  // a per-session budget in a ref, so re-renders can't restart it).
  const validationDeadlineRef = useRef<Map<string, number>>(new Map());
  // In-flight validation requests. `validationInFlightRef` holds the session ids
  // currently being checked so an effect restart (caused by ANOTHER session
  // resolving) cannot issue a second, overlapping request for the same session;
  // the controllers are aborted on unmount so nothing leaks (issue #37 r5).
  const validationRequestsRef = useRef<Set<AbortController>>(new Set());
  const validationInFlightRef = useRef<Set<string>>(new Set());
  // Every in-flight chunk-persistence promise (issue #37): stopAndUpload awaits
  // these so a stop-then-reload can't lose the final timeslices from IndexedDB.
  const pendingWritesRef = useRef<Promise<void>[]>([]);

  // Preflight a blob before upload; on failure surface a localized, safe message
  // and KEEP the local session (pending state) so the user can retry/discard.
  const blockUpload = useCallback(
    (args: {
      localId: string;
      workspaceId: string;
      messageKey: string;
      event: MeetingClientEventName;
      container?: string | null;
      chunkCount?: number | null;
      missingCount?: number | null;
      duplicateCount?: number | null;
    }): void => {
      void updateStoredSession(args.localId, { state: "pending-upload" });
      setPendingLocalId(args.localId);
      setUploadError(t(args.messageKey));
      // Operators must be able to SEE client-side rejections (issue #37): the
      // browser refuses this media before it ever reaches the worker, so the
      // server would otherwise have no signal at all. Best-effort, never blocks.
      const { family, major } = browserInfo();
      void assemblyDiagnostics(args.localId)
        .catch(() => null)
        .then((diag) =>
          reportClientEvent(args.workspaceId, {
            event: args.event,
            browser_family: family,
            browser_major: major,
            ...mediaTokens(args.container),
            chunk_count: args.chunkCount ?? null,
            missing_count: args.missingCount ?? null,
            duplicate_count: args.duplicateCount ?? null,
            max_gap_ms: diag?.max_gap_ms ?? null,
            max_media_gap_ms: diag?.max_media_gap_ms ?? null,
            max_skew_ms: diag?.max_skew_ms ?? null,
            total_bytes: diag?.total_bytes ?? null,
          }),
        );
    },
    [t],
  );

  const active = target !== null && recorder.status !== "idle";

  useEffect(() => {
    // Hydrate crash/reload leftovers once; expired local audio is purged first
    // (a clear local-retention policy, not an ever-growing browser store).
    let cancelled = false;
    (async () => {
      await purgeExpiredSessions();
      let rows: StoredRecordingSession[];
      try {
        rows = await listStoredSessions();
      } catch (error) {
        // "Could not look" is not "nothing to recover": saying the latter
        // would invite the user to record over a meeting still on disk.
        if (!cancelled && error instanceof RecordingStoreUnavailableError) {
          setUploadError(
            error instanceof RecordingStoreOutdatedError
              ? t("recordingBar.storageOutdated")
              : t("recordingBar.storageUnavailable"),
          );
        }
        return;
      }
      if (cancelled || rows.length === 0) return;
      // An already-uploaded session must NOT be offered as "resume upload" —
      // that would upload the same meeting twice. It goes to the validation
      // watcher instead (issue #37 review).
      const watch: StoredRecordingSession[] = [];
      const resume: StoredRecordingSession[] = [];
      for (const row of rows) {
        if (row.state === "awaiting-validation" && row.recordingId) {
          watch.push(row);
          continue;
        }
        // Crash window (issue #37 review): the server accepted completion but we
        // died before recording that locally. The server would 409 a second
        // completion, so RECONCILE — the backend creates the recording as
        // `uploading` and only moves it on at completion, so any other status
        // proves the upload already finished.
        if (row.state === "uploading" && row.recordingId) {
          try {
            const rec = await getRecording(row.workspaceId, row.recordingId);
            if (cancelled) return;
            if (rec.status !== "uploading") {
              await updateStoredSession(row.id, {
                state: "awaiting-validation",
              });
              watch.push({ ...row, state: "awaiting-validation" });
              continue;
            }
          } catch {
            // Offline / unreachable: fall through and offer resume, which is
            // the safe default (a redundant resume is recoverable; silently
            // dropping a recording is not).
          }
        }
        resume.push(row);
      }
      setRecoverable(resume);
      setAwaitingValidation(watch);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Watch uploaded recordings until the worker's media verdict lands: delete the
  // local copy once it is genuinely processed, KEEP it (with the safe reason) if
  // validation rejected the media (issue #37 review).
  //
  // Self-scheduling rather than setInterval: the next round is only queued once
  // the previous one has fully settled, so a slow response can never stack
  // overlapping polls. Each session is checked in PARALLEL with its own request
  // timeout, so one hanging request can't starve the others, and each carries an
  // absolute deadline held in a ref — a changing array must not silently restart
  // the budget (all three were issue #37 review findings).
  useEffect(() => {
    if (awaitingValidation.length === 0) return;
    let cancelled = false;
    let timer: number | undefined;
    const now = Date.now();
    for (const session of awaitingValidation) {
      if (!validationDeadlineRef.current.has(session.id)) {
        validationDeadlineRef.current.set(session.id, now + MAX_VALIDATION_MS);
      }
    }

    const stopWatching = (id: string) => {
      validationDeadlineRef.current.delete(id);
      setAwaitingValidation((rows) => rows.filter((r) => r.id !== id));
    };

    const checkOne = async (session: StoredRecordingSession) => {
      if (!session.recordingId) return;
      if (Date.now() > (validationDeadlineRef.current.get(session.id) ?? 0)) {
        // Budget spent. Stop asking, but KEEP the local copy — the next mount
        // re-checks; we never discard the user's only copy on a timeout.
        stopWatching(session.id);
        return;
      }
      // Already being checked by a round that outlived an effect restart.
      if (validationInFlightRef.current.has(session.id)) return;
      validationInFlightRef.current.add(session.id);
      const controller = new AbortController();
      validationRequestsRef.current.add(controller);
      const abortTimer = window.setTimeout(
        () => controller.abort(),
        VALIDATION_REQUEST_TIMEOUT_MS,
      );
      try {
        const rec = await getRecording(
          session.workspaceId,
          session.recordingId,
          {
            signal: controller.signal,
          },
        );
        if (cancelled) return;
        if (rec.status === "completed") {
          // Keep watching until the local copy is confirmed gone; otherwise
          // the session vanishes from our bookkeeping while its audio stays
          // on disk with nothing offering to remove it (r21).
          if (!(await discardLocal(session.id))) return;
          stopWatching(session.id);
        } else if (rec.status === "failed") {
          stopWatching(session.id);
          setRejected((rows) =>
            rows.some((r) => r.session.id === session.id)
              ? rows
              : [...rows, { session, code: rec.error_code }],
          );
          // The rejection itself is reported by the WORKER (issue #37 r7),
          // using the diagnostics stored with the upload. A client-submitted
          // verdict was forgeable (any member could attribute one to someone
          // else's recording) and replayed on every reload; the worker knows
          // the truth and emits it exactly once. The client only shows it.
        }
      } catch {
        // Transient (offline, 5xx, timeout): keep the copy, retry next round.
      } finally {
        window.clearTimeout(abortTimer);
        validationRequestsRef.current.delete(controller);
        validationInFlightRef.current.delete(session.id);
      }
    };

    const runRound = async () => {
      await Promise.allSettled(awaitingValidation.map(checkOne));
      if (!cancelled) {
        timer = window.setTimeout(() => void runRound(), VALIDATION_POLL_MS);
      }
    };
    void runRound();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [awaitingValidation]);

  // Abort any outstanding validation request on UNMOUNT only. Doing this in the
  // polling effect's cleanup would fire on every dependency change and simply
  // force the request to be re-issued (issue #37 review round 5).
  useEffect(
    () => () => {
      for (const controller of validationRequestsRef.current)
        controller.abort();
      validationRequestsRef.current.clear();
      validationInFlightRef.current.clear();
    },
    [],
  );

  // The guard covers every state with something to lose or interrupt: live
  // recording, a stopped-but-unuploaded meeting, and an active upload. (The
  // data itself now survives reload via IndexedDB — the warning prevents a
  // surprise interruption, not data loss.)
  const guard = active || uploading || pendingLocalId !== null;
  useEffect(() => {
    if (!guard) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [guard]);

  const start = useCallback(
    async (opts: StartRecordingOptions): Promise<boolean> => {
      setUploadError(null);
      setStartError(null);
      // The local session is created BEFORE the microphone is requested, so a
      // database that never opens stalls the whole start with nothing on
      // screen. A version upgrade blocked by an older tab does exactly that,
      // and IndexedDB waits forever rather than failing — so it is turned into
      // actionable guidance here (found in manual pod testing).
      let localId: string;
      try {
        localId = await createStoredSession({
          workspaceId: opts.workspaceId,
          title: opts.title,
          consent: opts.consent,
          requestedLanguage: opts.requestedLanguage ?? "auto",
          // Provisional; the recorder reports the container it actually chose
          // via onStart below, before any chunk lands (issue #37 review).
          mimeType: "audio/webm",
          clientFamily: browserInfo().family,
          clientMajor: browserInfo().major,
        });
      } catch (error) {
        setStartError(
          error instanceof RecordingStoreBlockedError
            ? t("recordModal.storageLockedByOtherTab")
            : getErrorMessage(error),
        );
        return false;
      }
      pendingWritesRef.current = [];
      emittedChunksRef.current = 0;
      const ok = await recorder.start({
        includeTabAudio: opts.includeTabAudio,
        deviceId: opts.deviceId,
        // Stay on this page after the tab is picked: the crop step is next.
        keepFocus: Boolean(opts.study),
        // Persist the REAL container immediately, so a crash before Stop still
        // recovers Ogg/MP4 correctly instead of being preflighted as WebM.
        onStart: (mimeType) => {
          void updateStoredSession(localId, { mimeType });
        },
        onChunk: (seq, chunk, meta) => {
          emittedChunksRef.current = Math.max(
            emittedChunksRef.current,
            seq + 1,
          );
          // Persistence must never stall the recorder callback, but we TRACK the
          // promise so stopAndUpload can await it. Intent FIRST (noteExpectedChunk)
          // so a lost data write still shows up as a gap (issue #37 review).
          pendingWritesRef.current.push(
            (async () => {
              await noteExpectedChunk(localId, seq);
              await appendChunk(localId, seq, chunk, meta);
            })().catch(() => {
              // Swallowed here by design (never break capture); the intent
              // record above is what makes the loss detectable at assembly.
            }),
          );
        },
      });
      if (ok) {
        localIdRef.current = localId;
        setTarget(opts);
        setStorageDegraded(false);
        // Hold the database open for the duration of the capture and learn
        // immediately if its writes start failing (issue #37 review r20).
        releaseCaptureRef.current?.();
        releaseCaptureRef.current = beginLiveCapture();
        setStorageFailureListener(() => setStorageDegraded(true));
        const display = recorder.getDisplayStream();
        if (opts.study && display) {
          try {
            await studyController.begin(
              {
                workspaceId: opts.workspaceId,
                notebookId: opts.study.notebookId,
                notebookTitle: opts.study.notebookTitle,
                language: opts.requestedLanguage ?? "auto",
                mode: opts.study.mode,
                localRecordingId: localId,
              },
              display,
            );
          } catch {
            // The recording itself is running; notes are simply unavailable.
          }
        }
      } else {
        await deleteStoredSession(localId).catch(() => {
          // Nothing was captured yet; a failed cleanup is not worth surfacing.
        });
      }
      return ok;
    },
    [recorder, t, studyController],
  );

  /** The right advice for a storage failure: an out-of-date tab can only be
   * fixed by reloading, everything else is worth retrying (issue #37 r22). */
  const storageMessage = useCallback(
    (error: unknown): string =>
      error instanceof RecordingStoreOutdatedError
        ? t("recordingBar.storageOutdated")
        : t("recordingBar.storageUnavailable"),
    [t],
  );

  /** Release the capture lease and its failure listener. Safe to call twice —
   * every exit from a capture goes through it, including the error paths. */
  const releaseCapture = useCallback(() => {
    releaseCaptureRef.current?.();
    releaseCaptureRef.current = null;
    setStorageFailureListener(null);
  }, []);

  const finishUpload = useCallback(
    async (args: {
      localId: string;
      workspaceId: string;
      title: string;
      consent: boolean;
      requestedLanguage?: RecordingLanguage;
      blob: Blob;
      filename: string;
      resumeFrom?: StoredRecordingSession;
      onUploaded?: (recordingId: string) => void;
    }): Promise<boolean> => {
      setUploading(true);
      setUploadError(null);
      setProgress(null);
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        // INSIDE the boundary: this mutation throws now, and a rejection here
        // used to skip the `finally` entirely — leaving `uploading` true and an
        // Uploading spinner that never ends. That is exactly what a deferred
        // upgrade completing after Stop produces (issue #37 review r22).
        await updateStoredSession(args.localId, { state: "uploading" });
      } catch (error) {
        abortRef.current = null;
        setUploading(false);
        setPendingLocalId(args.localId);
        setUploadError(storageMessage(error));
        return false;
      }
      try {
        const resume =
          args.resumeFrom?.uploadSessionId &&
          args.resumeFrom.partSizeBytes &&
          args.resumeFrom.recordingId
            ? {
                uploadSessionId: args.resumeFrom.uploadSessionId,
                recordingId: args.resumeFrom.recordingId,
                partSizeBytes: args.resumeFrom.partSizeBytes,
                uploadedParts: args.resumeFrom.uploadedParts,
              }
            : undefined;
        const diag = await assemblyDiagnostics(args.localId).catch(() => null);
        const storedForDiag = await getStoredSession(args.localId);
        const rec = await uploadRecording({
          workspaceId: args.workspaceId,
          blob: args.blob,
          // Recorder context travels WITH the upload (issue #37 r7) so the
          // worker can attach it to any media rejection it emits.
          clientDiagnostics: {
            ...(storedForDiag
              ? {
                  browser_family: producerBrowser(storedForDiag).family,
                  browser_major: producerBrowser(storedForDiag).major,
                }
              : {}),
            ...mediaTokens(storedForDiag?.mimeType, args.filename),
            chunk_count: diag?.chunk_count ?? null,
            max_gap_ms: diag?.max_gap_ms ?? null,
            max_media_gap_ms: diag?.max_media_gap_ms ?? null,
            max_skew_ms: diag?.max_skew_ms ?? null,
            total_bytes: diag?.total_bytes ?? null,
          },
          filename: args.filename,
          title: args.title,
          consent: args.consent,
          requestedLanguage: args.requestedLanguage ?? "auto",
          signal: controller.signal,
          resume,
          // Returned (not fire-and-forget) so the manager AWAITS persistence:
          // the server-session ids are durable before the first part is sent,
          // and the accepted-part manifest never lags the server (issue #37).
          onSession: (info) =>
            updateStoredSession(args.localId, {
              uploadSessionId: info.uploadSessionId,
              recordingId: info.recordingId,
              partSizeBytes: info.partSizeBytes,
            }),
          onPartDone: (partNumber, sha) =>
            recordUploadedPart(args.localId, partNumber, sha),
          onProgress: setProgress,
        });
        // Transport success is NOT proof of media validity (issue #37 review):
        // completion returns 202/pending and the worker validates the media
        // afterwards. KEEP the local copy until it confirms — otherwise a later
        // recording_invalid_container leaves the user with nothing to recover.
        await updateStoredSession(args.localId, {
          state: "awaiting-validation",
          recordingId: rec.id,
        });
        const stored = await getStoredSession(args.localId);
        if (stored) {
          setAwaitingValidation((rows) => [
            ...rows.filter((r) => r.id !== stored.id),
            stored,
          ]);
        }
        // The upload this recording was about has LANDED: an open Cancel
        // confirmation now refers to nothing it may destroy — the local copy
        // just became the until-the-worker-validates evidence. Invalidate the
        // dialog and the ref a stale confirm would otherwise resolve
        // (issue #37 review round 19).
        setConfirmingCancel(false);
        localIdRef.current = null;
        setPendingLocalId(null);
        setTarget(null);
        queryClient.invalidateQueries({
          queryKey: meetingKeys.list(args.workspaceId),
        });
        // The recording exists now: its study notes are tied to it.
        void linkStudySessions(args.localId, rec.id);
        args.onUploaded?.(rec.id);
        return true;
      } catch (e) {
        if ((e as DOMException)?.name === "AbortError") {
          // Cancel — but only a cancellation the SERVER CONFIRMED lets us
          // delete the user's only local copy. The server refuses cancel while
          // a completion holds a fresh lease (409), and that upload may still
          // finish; discarding the recording there would leave the user with
          // nothing while a recording quietly appears (issue #37 review r13).
          if (isUploadCancelled(e) && !e.serverConfirmed) {
            // Keep the local copy AND the bar: the session stays retryable and
            // the message explains why nothing was thrown away.
            await updateStoredSession(args.localId, {
              state: "pending-upload",
            });
            setPendingLocalId(args.localId);
            setUploadError(t("recordingBar.cancelUnconfirmed"));
            return false;
          }
          if (!(await discardLocal(args.localId))) {
            setPendingLocalId(args.localId);
            return false;
          }
          setPendingLocalId(null);
          setTarget(null);
          return false;
        }
        // A 409 on completion means the server ALREADY completed this upload —
        // a crash between its response and our state write, or a reconciliation
        // GET that failed and left us guessing. Retrying can never succeed, so
        // resolve it by asking what the recording became: it moves straight to
        // the validation watcher instead of looping on 409 forever (issue #37
        // review round 5).
        const stored409 = await getStoredSession(args.localId);
        const recordingId =
          stored409?.recordingId ?? args.resumeFrom?.recordingId;
        // A 409 is NOT proof of completion: the server also returns it for a
        // manifest/checksum mismatch and an expired or inactive session (issue
        // #37 review round 6). ASK the recording what actually happened — it is
        // created `uploading` and only moves on at completion, so any other
        // status means the upload really did finish. A genuine conflict falls
        // through to the normal error path instead of being falsely "recovered"
        // and then polling an `uploading` recording forever.
        // Ask the recording what actually happened. THREE outcomes matter, and
        // conflating them creates duplicate recordings (issue #37 review r8):
        //   moved on  -> the upload completed; watch for the verdict
        //   uploading -> the session really is dead; safe to restart
        //   unknown   -> the probe itself failed; we must NOT restart, because
        //                discarding the ids would upload the meeting twice.
        // A definite 404 belongs in `dead`, not `unknown`: the recording is
        // GONE — a cancellation that committed but whose 204 never arrived, or
        // an expiry sweep — so retrying against these ids can never succeed.
        // Mapping it to `unknown` kept the stale ids forever and every Retry
        // 404'd again (issue #37 review round 14). Only a TRANSIENT failure
        // (offline, 5xx) may stay unknown.
        const probe: "completed" | "dead" | "unknown" =
          isUploadSessionUnusable(e) && recordingId
            ? await getRecording(args.workspaceId, recordingId)
                .then((rec) =>
                  rec.status === "uploading"
                    ? ("dead" as const)
                    : ("completed" as const),
                )
                .catch((probeError) =>
                  isNotFound(probeError)
                    ? ("dead" as const)
                    : ("unknown" as const),
                )
            : "unknown";
        const alreadyCompleted = probe === "completed";
        if (alreadyCompleted && recordingId) {
          void linkStudySessions(args.localId, recordingId);
          await updateStoredSession(args.localId, {
            state: "awaiting-validation",
            recordingId,
          });
          const refreshed = await getStoredSession(args.localId);
          if (refreshed) {
            setAwaitingValidation((rows) => [
              ...rows.filter((r) => r.id !== refreshed.id),
              refreshed,
            ]);
          }
          setPendingLocalId(null);
          setTarget(null);
          setUploadError(null);
          queryClient.invalidateQueries({
            queryKey: meetingKeys.list(args.workspaceId),
          });
          return true;
        }
        setUploadError(getErrorMessage(e));
        if (probe === "dead") {
          // CONFIRMED dead: the server refused the session AND the recording is
          // still `uploading`, so the upload did not complete. Resuming would
          // fail forever, so DROP the server ids and let the next Retry open a
          // fresh session (issue #37 review r7). Only ever taken on a positive
          // answer — an indeterminate probe leaves the ids alone (r8).
          await updateStoredSession(args.localId, {
            state: "pending-upload",
            uploadSessionId: null,
            recordingId: null,
            partSizeBytes: null,
            uploadedParts: {},
          });
        } else {
          // Keep a session that already has a server recording RECONCILABLE:
          // downgrading it to `pending-upload` would hide it from the hydration
          // reconciliation and strand it (issue #37 review round 5).
          await updateStoredSession(args.localId, {
            state: recordingId ? "uploading" : "pending-upload",
          });
        }
        setPendingLocalId(args.localId);
        return false;
      } finally {
        setUploading(false);
        setProgress(null);
        abortRef.current = null;
      }
    },
    [queryClient],
  );

  // A capture that is never stopped — the tab navigates away, the provider
  // unmounts — must not hold the lease forever: every later version upgrade
  // in every tab would block on a recorder that no longer exists.
  useEffect(() => releaseCapture, [releaseCapture]);

  const stopAndUpload = useCallback(async () => {
    if (!target || !localIdRef.current) return;
    // An open study note ends at the last sample, before the tracks stop.
    await studyController.end("stopped");
    // The lease is held through stop, the write drain, AND the authoritative
    // state write below: `recorder.stop()` flushes a final `dataavailable`,
    // and releasing before it is durable lets another tab's upgrade close the
    // database out from under that last chunk (issue #37 review round 21).
    const { blob, mimeType } = await recorder.stop();
    const localId = localIdRef.current;
    // Await every persisted timeslice so the local copy is complete before we
    // (a) upload and (b) leave a recoverable session behind (issue #37).
    await Promise.allSettled(pendingWritesRef.current);
    pendingWritesRef.current = [];
    // Persist the authoritative emitted-chunk count from memory: it covers even
    // a chunk whose intent record failed to write (issue #37 review).
    try {
      await updateStoredSession(localId, {
        state: "pending-upload",
        mimeType,
        expectedChunks: emittedChunksRef.current,
      });
    } catch (error) {
      // The capture is over either way: hand the database back, keep the
      // session addressable so Retry still works, and say what happened
      // rather than leaving the dock mid-stop (issue #37 review round 22).
      releaseCapture();
      setPendingLocalId(localId);
      setUploadError(storageMessage(error));
      return;
    }
    // Everything the capture had to persist is now durable: hand the database
    // back so a deferred upgrade in another tab can finally proceed.
    releaseCapture();
    setPendingLocalId(localId);
    // Client preflight: refuse an obviously empty/malformed object up front,
    // preserving the local session, rather than uploading work that will only
    // fail in the worker (issue #37).
    const check = await preflightRecording(blob, mimeType);
    if (!check.ok) {
      blockUpload({
        localId,
        workspaceId: target.workspaceId,
        messageKey: PREFLIGHT_MESSAGE_KEYS[check.reason],
        event: PREFLIGHT_EVENTS[check.reason],
        container: mimeType,
        chunkCount: emittedChunksRef.current,
      });
      return;
    }
    await finishUpload({
      localId,
      workspaceId: target.workspaceId,
      title: target.title,
      consent: target.consent,
      requestedLanguage: target.requestedLanguage ?? "auto",
      blob,
      filename: `meeting.${extFromMime(mimeType)}`,
      onUploaded: target.onUploaded,
    });
  }, [
    recorder,
    target,
    finishUpload,
    blockUpload,
    releaseCapture,
    storageMessage,
    studyController,
  ]);

  const retryUpload = useCallback(async () => {
    if (!target || !pendingLocalId) return;
    let stored: StoredRecordingSession | undefined;
    let assembled;
    try {
      stored = await getStoredSession(pendingLocalId);
      if (!stored) return;
      assembled = await assembleRecording(pendingLocalId, stored.mimeType);
    } catch (error) {
      setUploadError(storageMessage(error));
      return;
    }
    const { blob, missing, duplicates } = assembled;
    if (missing.length > 0 || duplicates.length > 0) {
      blockUpload({
        localId: pendingLocalId,
        workspaceId: target.workspaceId,
        messageKey: "recordingBar.incomplete",
        event:
          duplicates.length > 0 ? "assembly_duplicate" : "assembly_incomplete",
        container: stored.mimeType,
        chunkCount: stored.expectedChunks,
        missingCount: missing.length,
        duplicateCount: duplicates.length,
      });
      return;
    }
    const check = await preflightRecording(blob, stored.mimeType);
    if (!check.ok) {
      blockUpload({
        localId: pendingLocalId,
        workspaceId: target.workspaceId,
        messageKey: PREFLIGHT_MESSAGE_KEYS[check.reason],
        event: PREFLIGHT_EVENTS[check.reason],
        container: stored.mimeType,
        chunkCount: stored.expectedChunks,
      });
      return;
    }
    await finishUpload({
      localId: pendingLocalId,
      workspaceId: target.workspaceId,
      title: target.title,
      consent: target.consent,
      requestedLanguage: target.requestedLanguage ?? "auto",
      blob,
      filename: `meeting.${extFromMime(stored.mimeType)}`,
      resumeFrom: stored,
      onUploaded: target.onUploaded,
    });
  }, [target, pendingLocalId, finishUpload, blockUpload]);

  /** Delete the local copy, surfacing a failure instead of pretending the
   * recording was discarded when it is still on disk (issue #37 review r20). */
  const discardLocal = useCallback(
    async (localId: string): Promise<boolean> => {
      try {
        await deleteStoredSession(localId);
        // The recording is discarded: the notes taken in it go with it.
        void discardStudySessions(localId);
        return true;
      } catch (error) {
        setUploadError(storageMessage(error));
        return false;
      }
    },
    [storageMessage],
  );

  const cancel = useCallback(async () => {
    // TRUE cancel: abort an in-flight upload (the manager DELETEs the server
    // session, discarding its parts) and remove the local data.
    const localId = localIdRef.current ?? pendingLocalId;
    // FENCE against a stale confirmation (issue #37 review round 19): the
    // dialog can be confirmed AFTER the upload it was about succeeded. The
    // session is then `awaiting-validation` — its local copy is the retained
    // until-the-worker-validates evidence, and "target is gone" used to read
    // as "confirmed", deleting that copy with no server DELETE at all. A
    // cancel whose subject moved on destroys NOTHING.
    if (localId) {
      const staleCheck = await getStoredSession(localId);
      if (staleCheck?.state === "awaiting-validation") {
        localIdRef.current = null;
        setPendingLocalId(null);
        setTarget(null);
        return;
      }
    }
    const wasUploading = abortRef.current !== null;
    abortRef.current?.abort();
    await studyController.end("stopped");
    recorder.cancel();
    releaseCapture();
    localIdRef.current = null;
    if (localId && !wasUploading) {
      // No upload in flight, but a failed run may have left a server session.
      // The local copy may only be dropped once the server CONFIRMS the session
      // is gone: it refuses cancellation while a completion holds a fresh lease
      // (409), and that upload may still land (issue #37 review round 13).
      // The workspace comes from the STORED session, never from `target` —
      // and a session with a server id is NEVER presumed cancelled just
      // because the in-memory target is gone (review round 19).
      const stored = await getStoredSession(localId);
      const confirmed = stored?.uploadSessionId
        ? await tryAbortUploadSession(
            stored.workspaceId,
            stored.uploadSessionId,
          )
        : true;
      if (confirmed) {
        // The UI may only forget this recording once its audio is GONE. A
        // failed delete keeps the bar (and its message) so the user can retry
        // rather than believing it was discarded (issue #37 review r21).
        if (!(await discardLocal(localId))) {
          setPendingLocalId(localId);
          return;
        }
      } else {
        // Same rule as the in-flight path: keep the recording AND the bar, so
        // the session stays retryable and the message explains why nothing was
        // thrown away.
        await updateStoredSession(localId, { state: "pending-upload" });
        setPendingLocalId(localId);
        setUploadError(t("recordingBar.cancelUnconfirmed"));
        return;
      }
    }
    setPendingLocalId(null);
    setTarget(null);
    setUploadError(null);
  }, [recorder, target, pendingLocalId, t, discardLocal, studyController]);

  const pauseRecording = useCallback(() => {
    recorder.pause();
    void studyController.pause();
  }, [recorder, studyController]);
  const resumeRecording = useCallback(() => {
    recorder.resume();
    void studyController.resume();
  }, [recorder, studyController]);

  /** "Change source": share another tab and bind the next notes to its
   * notebook. The meeting recording keeps running. */
  const changeStudySource = useCallback(
    async (next: StudyTarget): Promise<boolean> => {
      const localId = localIdRef.current;
      if (!target || !localId) return false;
      const display = await recorder.replaceDisplay(true);
      if (!display) return false;
      await studyController.begin(
        {
          workspaceId: target.workspaceId,
          notebookId: next.notebookId,
          notebookTitle: next.notebookTitle,
          language: target.requestedLanguage ?? "auto",
          mode: next.mode,
          localRecordingId: localId,
        },
        display,
      );
      return true;
    },
    [recorder, studyController, target],
  );

  const resumeRecovered = useCallback(
    async (stored: StoredRecordingSession) => {
      setRecoverable((rows) => rows.filter((r) => r.id !== stored.id));
      let assembled;
      try {
        assembled = await assembleRecording(stored.id, stored.mimeType);
      } catch (error) {
        // A storage failure yields no chunks — which downstream would read as
        // "nothing was recorded" and DELETE the session. Preserve it, put it
        // back in the recoverable list, and say what to do about it
        // (issue #37 review rounds 20/22).
        setRecoverable((rows) => [
          ...rows.filter((r) => r.id !== stored.id),
          stored,
        ]);
        setUploadError(storageMessage(error));
        return;
      }
      const { blob, missing, duplicates } = assembled;
      // A gap or a malformed container: keep the session and tell the user, via
      // the bar (recoveredTarget), instead of uploading a known-bad object (#37).
      const recoveredTarget = {
        workspaceId: stored.workspaceId,
        title: stored.title,
        includeTabAudio: false,
        consent: stored.consent,
        requestedLanguage: stored.requestedLanguage ?? "auto",
      };
      // Integrity BEFORE the empty check: a session whose chunk writes were all
      // lost has 0 bytes but a non-empty `missing` list, and must be reported —
      // not silently deleted as "nothing was recorded" (issue #37 review).
      if (missing.length > 0 || duplicates.length > 0) {
        setTarget(recoveredTarget);
        blockUpload({
          localId: stored.id,
          workspaceId: stored.workspaceId,
          messageKey: "recordingBar.incomplete",
          event:
            duplicates.length > 0
              ? "assembly_duplicate"
              : "assembly_incomplete",
          container: stored.mimeType,
          chunkCount: stored.expectedChunks,
          missingCount: missing.length,
          duplicateCount: duplicates.length,
        });
        return;
      }
      if (blob.size === 0) {
        // Genuinely empty (assembly SUCCEEDED and found nothing) — but the row
        // only disappears once the delete is confirmed; a failed one goes back
        // on the recoverable list with the error showing.
        if (!(await discardLocal(stored.id))) {
          setRecoverable((rows) => [
            ...rows.filter((r) => r.id !== stored.id),
            stored,
          ]);
        }
        return;
      }
      const check = await preflightRecording(blob, stored.mimeType);
      if (!check.ok) {
        setTarget(recoveredTarget);
        blockUpload({
          localId: stored.id,
          workspaceId: stored.workspaceId,
          messageKey: PREFLIGHT_MESSAGE_KEYS[check.reason],
          event: PREFLIGHT_EVENTS[check.reason],
          container: stored.mimeType,
          chunkCount: stored.expectedChunks,
        });
        return;
      }
      setPendingLocalId(stored.id);
      const ok = await finishUpload({
        localId: stored.id,
        workspaceId: stored.workspaceId,
        title: stored.title,
        consent: stored.consent,
        requestedLanguage: stored.requestedLanguage ?? "auto",
        blob,
        filename: `meeting.${extFromMime(stored.mimeType)}`,
        resumeFrom: stored,
      });
      // Recovery outcome is one of #37's required client signals.
      const producer = producerBrowser(stored);
      void reportClientEvent(stored.workspaceId, {
        event: ok ? "upload_recovered" : "upload_recovery_failed",
        browser_family: producer.family,
        browser_major: producer.major,
        ...mediaTokens(stored.mimeType),
        chunk_count: stored.expectedChunks,
      });
      if (!ok) {
        // Failed (not cancelled): the bar's retry takes over via
        // pendingLocalId; give it the context it needs.
        setTarget(recoveredTarget);
      }
    },
    [finishUpload, blockUpload],
  );

  /** Drop a local copy the user no longer wants after the worker rejected the
   * media (the only way it leaves the browser — we never delete it for them). */
  const discardRejected = useCallback(
    async (stored: StoredRecordingSession) => {
      // Remove the row only once the audio is actually GONE: dropping it first
      // tells the user the recording was discarded while it is still on disk
      // (issue #37 review round 21). A failure keeps the row — and its Discard
      // button — so the retry path stays visible.
      if (!(await discardLocal(stored.id))) return;
      setRejected((rows) => rows.filter((r) => r.session.id !== stored.id));
    },
    [discardLocal],
  );

  const discardRecovered = useCallback(
    async (stored: StoredRecordingSession) => {
      if (stored.uploadSessionId) {
        try {
          await abortUploadSession(stored.workspaceId, stored.uploadSessionId);
        } catch {
          // Expiry sweep cleans up.
        }
      }
      // Only then does the row go: see discardRejected.
      if (!(await discardLocal(stored.id))) return;
      setRecoverable((rows) => rows.filter((r) => r.id !== stored.id));
    },
    [discardLocal],
  );

  const value = useMemo(
    () => ({
      activeWorkspaceId: active ? (target?.workspaceId ?? null) : null,
      isSupported: recorder.isSupported,
      start,
      error: recorder.error,
      /** A start attempt that failed BEFORE the recorder ran (e.g. the local
       * database is locked by another tab). Distinct from `error`, which is
       * always a capture/microphone problem. */
      startError,
      study,
      studyPreview: () => studyController.previewFrame(),
      studyCrop: () => studyController.crop,
      setStudyCrop: (crop: Crop) => studyController.setCrop(crop),
      openStudyFloat: () => void openFloating(),
    }),
    [
      study,
      studyController,
      openFloating,
      active,
      target?.workspaceId,
      recorder.isSupported,
      recorder.error,
      start,
      startError,
    ],
  );

  const pending = pendingLocalId !== null && !uploading;
  const showRecoverable =
    recoverable.length > 0 && !active && !uploading && !pending;
  const dockVisible =
    rejected.length > 0 ||
    showRecoverable ||
    active ||
    uploading ||
    pending ||
    // A storage failure can happen with nothing recording — discarding a
    // recovered row, say. The dock still has to open, or the explanation has
    // nowhere to appear (issue #37 review round 21).
    uploadError !== null;
  const dockStatus = uploading
    ? ("uploading" as const)
    : pending
      ? ("retry" as const)
      : recorder.status === "paused"
        ? ("paused" as const)
        : ("recording" as const);
  const cancelConfirmLabelId = "recording-cancel-confirm-title";

  // A bottom-positioned INLINE drawer: the panel reserves layout space in the
  // application shell, so the recorder can never cover page content or escape
  // the viewport — and being app-level, it survives every route change.
  return (
    <RecordingContext.Provider value={value}>
      <div
        className={cn(
          "app-backdrop flex h-dvh min-h-0 flex-col",
          "recording-dock-shell",
        )}
      >
        <>
          <div
            className={cn(
              "min-h-0 flex-1 overflow-hidden",
              "recording-dock-content",
            )}
          >
            {children}
          </div>
          {dockVisible ? (
            <section
              className={cn(
                "glass-chrome max-h-[calc(100dvh-8rem)] shrink-0 overflow-y-auto border-t",
                "recording-dock-panel",
              )}
              aria-label={t("recordingBar.regionLabel")}
            >
              <div className={cn("p-4", "recording-dock")}>
                <div className="flex flex-col gap-4">
                  {/* The worker rejected the uploaded media: the local copy
                        is KEPT (issue #37) with the safe localized reason. */}
                  {rejected.map(({ session, code }) => (
                    <div key={session.id}>
                      <div className="flex flex-wrap items-center gap-2">
                        <div className="recording-dock-title-cell">
                          <Alert
                            variant="warning"
                            isInline={true}
                            title={t("recordingBar.rejectedTitle", {
                              title: session.title,
                            })}
                          >
                            {meetingErrorMessage(code, t)}{" "}
                            {t("recordingBar.rejectedKept")}
                          </Alert>
                        </div>
                        <div>
                          <Button
                            onClick={() => discardRejected(session)}
                            variant="destructive"
                            size="sm"
                          >
                            {t("recordingBar.recoveredDiscard")}
                          </Button>
                        </div>
                      </div>
                    </div>
                  ))}
                  {showRecoverable &&
                    recoverable.map((stored) => (
                      <div key={stored.id}>
                        <div className="flex flex-wrap items-center gap-2">
                          <div className="recording-dock-title-cell">
                            <p
                              className={cn("app-copy", "recording-dock-title")}
                            >
                              <strong>
                                {t("recordingBar.recoveredTitle", {
                                  title: stored.title,
                                })}
                              </strong>
                            </p>
                          </div>
                          <div>
                            <Button
                              onClick={() => resumeRecovered(stored)}
                              variant="default"
                              size="sm"
                            >
                              {t("recordingBar.recoveredResume")}
                            </Button>
                          </div>
                          <div>
                            <Button
                              onClick={() => discardRecovered(stored)}
                              variant="destructive"
                              size="sm"
                            >
                              {t("recordingBar.recoveredDiscard")}
                            </Button>
                          </div>
                        </div>
                      </div>
                    ))}
                  {!active && !uploading && !pending && uploadError && (
                    <div>
                      <Alert
                        variant="danger"
                        isInline={true}
                        title={uploadError}
                      />
                    </div>
                  )}
                  {(active || uploading || pending) && (
                    <div>
                      <RecordingDock
                        status={dockStatus}
                        title={target?.title ?? t("recordModal.title")}
                        durationMs={recorder.durationMs}
                        level={recorder.level}
                        showLevel={recorder.status !== "idle"}
                        trackWarning={recorder.trackWarning}
                        noTabAudio={
                          (target?.includeTabAudio ?? false) &&
                          recorder.tabAudioPresent === false
                        }
                        uploadProgress={uploading ? progress : null}
                        uploadError={uploadError}
                        storageDegraded={storageDegraded}
                        onStop={() => void stopAndUpload()}
                        onRetry={() => void retryUpload()}
                        onPause={pauseRecording}
                        onResume={resumeRecording}
                        study={
                          study.active && active
                            ? {
                                status: study,
                                onToggle: () => void studyController.toggle(),
                                onViewNotes: () =>
                                  navigate(
                                    `/workspaces/${study.workspaceId}/study/${study.notebookId}`,
                                  ),
                                onChangeSource: () => setChangingSource(true),
                                floating: floating.supported
                                  ? {
                                      isOpen: floating.isOpen,
                                      onToggle: () =>
                                        floating.isOpen
                                          ? floating.close()
                                          : void floating.open(),
                                    }
                                  : undefined,
                              }
                            : undefined
                        }
                        onCancelRequest={() => setConfirmingCancel(true)}
                        busy={uploading}
                      />
                    </div>
                  )}
                </div>
              </div>
            </section>
          ) : undefined}
        </>
      </div>
      <Dialog
        open={confirmingCancel}
        onOpenChange={(open) => {
          if (!open) (() => setConfirmingCancel(false))();
        }}
      >
        <DialogContent
          aria-labelledby={cancelConfirmLabelId}
          aria-describedby="recording-cancel-confirm-body"
          className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
        >
          <DialogHeader>
            <DialogTitle id={cancelConfirmLabelId}>
              {t("recordingBar.cancelConfirmTitle")}
            </DialogTitle>
          </DialogHeader>
          <div id="recording-cancel-confirm-body" className="min-w-0">
            {t("recordingBar.cancelConfirmBody")}
            {study.count > 0 && (
              <> {t("study:dock.cancelAlsoNotes", { count: study.count })}</>
            )}
          </div>
          <DialogFooter>
            <Button
              onClick={() => {
                setConfirmingCancel(false);
                void cancel();
              }}
              variant="destructive"
            >
              {t("recordingBar.cancelConfirmDiscard")}
            </Button>
            <Button onClick={() => setConfirmingCancel(false)} variant="link">
              {t("recordingBar.cancelConfirmKeep")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {floating.container &&
        study.active &&
        createPortal(
          <FloatingNotePanel
            status={study}
            recording={recorder.status === "recording" && !uploading}
            paused={recorder.status === "paused"}
            durationMs={recorder.durationMs}
            onToggle={() => void studyController.toggle()}
          />,
          floating.container,
        )}
      {changingSource && target && (
        <ChangeSourceDialog
          workspaceId={target.workspaceId}
          mode={study.mode}
          onClose={() => setChangingSource(false)}
          onChange={changeStudySource}
        />
      )}
    </RecordingContext.Provider>
  );
}
