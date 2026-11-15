/**
 * Study notes during a recording: one state machine for the Take note button
 * (in the recording bar and in the floating window).
 *
 * A press is one transition: `idle → open` (frame saved, look-back taken) or
 * `open → idle` (the note is complete). Presses run one at a time, so two
 * clicks can never act on the same state twice.
 *
 * Everything is written to IndexedDB before it is sent. Uploading is a
 * separate, repeatable step that also runs after a reload.
 */

import axios from "axios";

import * as studyApi from "@/api/study";
import {
  type Crop,
  FrameGrabber,
  frameToPng,
  FULL_FRAME,
} from "@/lib/study/frame";
import * as store from "@/lib/study/noteStore";
import type {
  StoredNote,
  StoredStudySession,
  StudyMode,
} from "@/lib/study/noteStore";
import {
  encodeWav,
  NOTE_SAMPLE_RATE,
  samplesToMs,
  TabAudioBuffer,
} from "@/lib/study/tabAudio";

/** A note left open is closed by itself after this long (STT upload limit). */
export const MAX_NOTE_MS = 10 * 60 * 1000;
const RETRY_UPLOAD_MS = 5000;

export type EndReason =
  | "paused"
  | "stopped"
  | "source_changed"
  | "sharing_lost"
  | "max_length"
  | "interrupted";

/** What one press did. */
export type PressResult = {
  outcome: "started" | "saved" | "error";
  reason?: string;
  captured_ms?: number;
  audio_start_ms?: number | null;
  audio_end_ms?: number | null;
  has_audio?: boolean;
};

/** A frame that is held in memory, not yet encoded. */
export type PinnedFrame = { encode: (crop: Crop) => Promise<Blob> };

/** What the shared tab gives us: frames and (maybe) audio. */
export type CaptureSource = {
  /** Null when the tab was shared without sound. */
  buffer: TabAudioBuffer | null;
  hasVideo: boolean;
  isLive: () => boolean;
  /** Pin the newest frame. Resolves as soon as the frame is held; encoding
   * it (slow for a large frame) is the separate `encode` step. */
  grab: () => Promise<PinnedFrame>;
  onLost: (listener: () => void) => void;
  close: () => void;
};

export type StudyStatus = {
  active: boolean;
  sessionId: string | null;
  workspaceId: string | null;
  notebookId: string | null;
  notebookTitle: string | null;
  mode: StudyMode;
  /** Recording-clock time the open note was taken at; null when none is open. */
  openSinceMs: number | null;
  /** Notes taken in this recording. */
  count: number;
  /** Notes not yet confirmed by the server. */
  waiting: number;
  audioAvailable: boolean;
  videoAvailable: boolean;
  /** Why the last open note ended by itself, or the last press failed. */
  notice: string | null;
  /** Bumped on every finished capture (for "saved" announcements). */
  savedTick: number;
};

export const IDLE_STUDY: StudyStatus = {
  active: false,
  sessionId: null,
  workspaceId: null,
  notebookId: null,
  notebookTitle: null,
  mode: "until_stopped",
  openSinceMs: null,
  count: 0,
  waiting: 0,
  audioAvailable: false,
  videoAvailable: false,
  notice: null,
  savedTick: 0,
};

export type BeginOptions = {
  workspaceId: string;
  notebookId: string;
  notebookTitle: string;
  language: string;
  mode: StudyMode;
  localRecordingId: string;
};

export type ControllerDeps = {
  /** Elapsed recording time in milliseconds ("in this recording"). */
  clock: () => number;
  isPaused: () => boolean;
  onChange: (status: StudyStatus) => void;
  createSource?: (display: MediaStream) => Promise<CaptureSource>;
  api?: Pick<
    typeof studyApi,
    "putSession" | "putNote" | "linkSession" | "deleteSession"
  >;
  newId?: () => string;
};

type OpenNote = {
  id: string;
  capturedMs: number;
  audioStartMs: number;
  writes: Promise<void>[];
  /** The note's record (with its image) is in IndexedDB. */
  stored: Promise<void>;
  timer: ReturnType<typeof setTimeout> | null;
};

/** The real capture source: an audio tap for the tab's own audio track (the
 * microphone is never connected to it) and a frame grabber for its video. */
export async function createCaptureSource(
  display: MediaStream,
): Promise<CaptureSource> {
  const audioTrack = display.getAudioTracks()[0] ?? null;
  const videoTrack = display.getVideoTracks()[0] ?? null;
  const grabber = videoTrack ? new FrameGrabber(videoTrack) : null;
  let context: AudioContext | null = null;
  let buffer: TabAudioBuffer | null = null;
  if (audioTrack) {
    try {
      // A 16 kHz context: the browser resamples, notes stay small.
      context = new AudioContext({ sampleRate: NOTE_SAMPLE_RATE });
      await context.audioWorklet.addModule(
        `${import.meta.env.BASE_URL}study-tap.worklet.js`,
      );
      const tap = new AudioWorkletNode(context, "study-tap");
      const active = new TabAudioBuffer(context.sampleRate);
      tap.port.onmessage = (event: MessageEvent<Float32Array>) =>
        active.append(event.data);
      context
        .createMediaStreamSource(new MediaStream([audioTrack]))
        .connect(tap);
      // A silent path to the output keeps the graph being processed.
      const mute = context.createGain();
      mute.gain.value = 0;
      tap.connect(mute).connect(context.destination);
      void context.resume().catch(() => {});
      buffer = active;
    } catch {
      void context?.close().catch(() => {});
      context = null;
      buffer = null;
    }
  }
  return {
    buffer,
    hasVideo: grabber !== null,
    isLive: () => grabber?.live ?? false,
    grab: async () => {
      if (!grabber) throw new Error("No video is shared");
      const frame = await grabber.grab();
      return {
        encode: async (crop) => {
          try {
            return await frameToPng(frame, crop);
          } finally {
            frame.close();
          }
        },
      };
    },
    onLost: (listener) => {
      videoTrack?.addEventListener("ended", listener);
    },
    close: () => {
      grabber?.close();
      void context?.close().catch(() => {});
    },
  };
}

function isGone(error: unknown): boolean {
  if (!axios.isAxiosError(error)) return false;
  const status = error.response?.status;
  // The server will never accept this note (its session was discarded, the
  // file is invalid): repeating the upload cannot help.
  return status === 404 || status === 409 || status === 413 || status === 422;
}

// --- uploading (also after a reload) -----------------------------------------

const defaultApi = studyApi;
let flushing: Promise<void> | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
const knownSessions = new Set<string>();
const flushListeners = new Set<() => void>();

async function ensureServerSession(
  api: NonNullable<ControllerDeps["api"]>,
  session: StoredStudySession,
): Promise<void> {
  if (knownSessions.has(session.id)) return;
  await api.putSession(session.workspaceId, session.id, {
    notebook_id: session.notebookId,
    language: session.language,
    mode: session.mode,
    started_at: session.startedAt,
  });
  knownSessions.add(session.id);
}

async function flushOnce(
  api: NonNullable<ControllerDeps["api"]>,
): Promise<boolean> {
  let allDone = true;
  const sessions = new Map(
    (await store.listSessions()).map((session) => [session.id, session]),
  );
  for (const note of await store.listNotesInState("queued")) {
    const session = sessions.get(note.sessionId);
    if (!session || !note.image) {
      await store.markUploaded(note.id);
      continue;
    }
    try {
      await ensureServerSession(api, session);
      await api.putNote(note.workspaceId, note.id, {
        sessionId: note.sessionId,
        seq: note.seq,
        capturedMs: note.capturedMs,
        image: note.image,
        audio: note.hasAudio ? note.audio : null,
        audioStartMs: note.audioStartMs,
        audioEndMs: note.audioEndMs,
        endReason: note.endReason,
      });
      await store.markUploaded(note.id);
    } catch (error) {
      if (isGone(error)) await store.markUploaded(note.id);
      else allDone = false;
    }
    flushListeners.forEach((listener) => listener());
  }
  for (const session of sessions.values()) {
    if (session.recordingId && !session.linked) {
      try {
        await ensureServerSession(api, session);
        await api.linkSession(
          session.workspaceId,
          session.id,
          session.recordingId,
        );
        await store.updateSession(session.id, { linked: true });
        session.linked = true;
      } catch (error) {
        if (isGone(error)) {
          await store.updateSession(session.id, { linked: true });
          session.linked = true;
        } else allDone = false;
      }
    }
    if (session.ended && session.linked) {
      const left = (await store.listNotes(session.id)).some(
        (note) => note.state !== "uploaded",
      );
      if (!left) await store.deleteSession(session.id);
    }
  }
  return allDone;
}

/** Send everything that is waiting. Safe to call at any time, from anywhere. */
export function flushStudyNotes(
  api: NonNullable<ControllerDeps["api"]> = defaultApi,
): Promise<void> {
  if (!store.isStudyStoreAvailable()) return Promise.resolve();
  if (!flushing) {
    flushing = (async () => {
      let done = false;
      try {
        done = await flushOnce(api);
      } catch {
        done = false;
      } finally {
        flushing = null;
      }
      if (!done && retryTimer === null) {
        retryTimer = setTimeout(() => {
          retryTimer = null;
          void flushStudyNotes(api);
        }, RETRY_UPLOAD_MS);
      }
    })();
  }
  return flushing;
}

/** A note that was still open when the page went away keeps what it has. */
async function closeInterrupted(note: StoredNote): Promise<void> {
  const parts = await store.readAudio(note.id);
  const samples = parts.reduce((sum, part) => sum + part.length, 0);
  await store.putNote({
    ...note,
    state: "queued",
    audio: samples > 0 ? encodeWav(parts, note.sampleRate) : null,
    hasAudio: samples > 0,
    audioEndMs:
      note.audioStartMs === null
        ? null
        : note.audioStartMs + samplesToMs(samples, note.sampleRate),
    endReason: "interrupted",
  });
}

/** After a reload: finish notes that were open and send what is waiting. */
export async function recoverStudyNotes(
  api: NonNullable<ControllerDeps["api"]> = defaultApi,
): Promise<void> {
  if (!store.isStudyStoreAvailable()) return;
  try {
    for (const note of await store.listNotesInState("open"))
      await closeInterrupted(note);
    for (const session of await store.listSessions())
      if (!session.ended)
        await store.updateSession(session.id, { ended: true });
  } catch {
    return;
  }
  await flushStudyNotes(api);
}

/** The meeting recording now exists on the server: tie its sessions to it. */
export async function linkStudySessions(
  localRecordingId: string,
  recordingId: string,
  api: NonNullable<ControllerDeps["api"]> = defaultApi,
): Promise<void> {
  if (!store.isStudyStoreAvailable()) return;
  try {
    for (const session of await store.listSessions()) {
      if (session.localRecordingId === localRecordingId)
        await store.updateSession(session.id, { recordingId, ended: true });
    }
  } catch {
    return;
  }
  await flushStudyNotes(api);
}

/** The recording was discarded: its notes go too, here and on the server. */
export async function discardStudySessions(
  localRecordingId: string,
  api: NonNullable<ControllerDeps["api"]> = defaultApi,
): Promise<void> {
  if (!store.isStudyStoreAvailable()) return;
  let sessions: StoredStudySession[];
  try {
    sessions = (await store.listSessions()).filter(
      (session) => session.localRecordingId === localRecordingId,
    );
  } catch {
    return;
  }
  for (const session of sessions) {
    await store.deleteSession(session.id).catch(() => {});
    try {
      await api.deleteSession(session.workspaceId, session.id);
    } catch {
      // The session never reached the server, or the request failed: its
      // notes are gone locally and nothing more will be sent for it.
    }
    knownSessions.delete(session.id);
  }
}

/** How many notes the stored sessions of a recording hold. */
export async function countStudyNotes(
  localRecordingId: string,
): Promise<number> {
  if (!store.isStudyStoreAvailable()) return 0;
  try {
    let count = 0;
    for (const session of await store.listSessions()) {
      if (session.localRecordingId === localRecordingId)
        count += (await store.listNotes(session.id)).length;
    }
    return count;
  } catch {
    return 0;
  }
}

// --- the controller ----------------------------------------------------------

export class StudyNoteController {
  private status: StudyStatus = IDLE_STUDY;
  private session: StoredStudySession | null = null;
  private source: CaptureSource | null = null;
  private open: OpenNote | null = null;
  private chain: Promise<unknown> = Promise.resolve();
  private readonly api: NonNullable<ControllerDeps["api"]>;
  private readonly newId: () => string;
  private readonly onFlush = () => void this.refreshCounts();

  constructor(private readonly deps: ControllerDeps) {
    this.api = deps.api ?? defaultApi;
    this.newId = deps.newId ?? (() => crypto.randomUUID());
  }

  get current(): StudyStatus {
    return this.status;
  }

  private set(patch: Partial<StudyStatus>): void {
    this.status = { ...this.status, ...patch };
    this.deps.onChange(this.status);
  }

  /** Run one transition at a time, in the order they were asked for. */
  private serialized<T>(work: () => Promise<T>): Promise<T> {
    const run = this.chain.then(work, work);
    this.chain = run.catch(() => undefined);
    return run;
  }

  private async refreshCounts(): Promise<void> {
    const session = this.session;
    if (!session) return;
    try {
      let count = 0;
      let waiting = 0;
      for (const stored of await store.listSessions()) {
        if (stored.localRecordingId !== session.localRecordingId) continue;
        for (const note of await store.listNotes(stored.id)) {
          count += 1;
          if (note.state !== "uploaded") waiting += 1;
        }
      }
      if (this.session) this.set({ count, waiting });
    } catch {
      // Counts are informative only.
    }
  }

  /** Start taking notes from a shared tab into a notebook. */
  async begin(options: BeginOptions, display: MediaStream): Promise<void> {
    await this.serialized(async () => {
      await this.detach("source_changed");
      const session: StoredStudySession = {
        id: this.newId(),
        workspaceId: options.workspaceId,
        notebookId: options.notebookId,
        notebookTitle: options.notebookTitle,
        language: options.language,
        mode: options.mode,
        crop: FULL_FRAME,
        nextSeq: 0,
        startedAt: new Date().toISOString(),
        localRecordingId: options.localRecordingId,
        recordingId: null,
        linked: false,
        ended: false,
      };
      await store.putSession(session);
      this.session = session;
      const source = await (this.deps.createSource ?? createCaptureSource)(
        display,
      );
      this.source = source;
      source.buffer?.setGate(!this.deps.isPaused());
      source.onLost(() => {
        void this.serialized(async () => {
          if (this.source !== source) return;
          await this.finishOpen("sharing_lost");
          this.set({ videoAvailable: false, notice: "sharing_lost" });
        });
      });
      flushListeners.add(this.onFlush);
      this.set({
        ...IDLE_STUDY,
        active: true,
        sessionId: session.id,
        workspaceId: session.workspaceId,
        notebookId: session.notebookId,
        notebookTitle: session.notebookTitle,
        mode: session.mode,
        audioAvailable: source.buffer !== null,
        videoAvailable: source.hasVideo,
        savedTick: this.status.savedTick,
      });
      await this.refreshCounts();
      void ensureServerSession(this.api, session).catch(() => {});
    });
  }

  /** Which part of the frame to keep (normalized capture coordinates). */
  async setCrop(crop: Crop): Promise<void> {
    const session = this.session;
    if (!session) return;
    session.crop = crop;
    await store.updateSession(session.id, { crop });
  }

  get crop(): Crop {
    return this.session?.crop ?? FULL_FRAME;
  }

  /** A still of the whole shared frame, for choosing the video area. */
  previewFrame(): Promise<Blob> {
    if (!this.source) return Promise.reject(new Error("Nothing is shared"));
    return this.source.grab().then((frame) => frame.encode(FULL_FRAME));
  }

  /**
   * One press of Take note / Stop note. The decision and the pinning (frame
   * held, look-back taken, state changed) happen in turn, one press at a
   * time, and take a few tens of milliseconds. Encoding and storing the image
   * then finish outside the queue, so the next press is not held up.
   */
  async toggle(): Promise<PressResult> {
    let result: PressResult;
    try {
      const pending = await this.serialized(async () => ({
        settled: await this.decide(),
      }));
      result = await pending.settled;
    } catch {
      result = { outcome: "error", reason: "storage" };
    }
    this.set({
      notice: result.outcome === "error" ? (result.reason ?? null) : null,
    });
    return result;
  }

  /** Stop the open note. */
  stopNote(): Promise<void> {
    return this.serialized(async () => {
      await this.finishOpen(null);
    });
  }

  /** Runs inside the queue. Returns the rest of the work as a promise. */
  private async decide(): Promise<Promise<PressResult> | PressResult> {
    if (!this.session || !this.source)
      return { outcome: "error", reason: "not_recording" };
    if (this.open) return this.finishOpen(null);
    if (this.deps.isPaused()) return { outcome: "error", reason: "paused" };
    if (!this.source.hasVideo || !this.source.isLive())
      return { outcome: "error", reason: "sharing_lost" };
    return this.capture();
  }

  /**
   * Pin a capture (inside the queue), and return the storing of it as a
   * promise that the queue does not wait for.
   */
  private async capture(): Promise<Promise<PressResult>> {
    const session = this.session as StoredStudySession;
    const source = this.source as CaptureSource;
    const capturedMs = Math.max(0, Math.round(this.deps.clock()));
    const id = this.newId();
    const buffer = source.buffer;
    // Pin the frame and the end of the look-back together, before any
    // encoding or storage work.
    const pinning = source.grab();
    const keepOpen = session.mode === "until_stopped" && buffer !== null;
    const writes: Promise<void>[] = [];
    let lookbackSamples = 0;
    let lookback: Float32Array | null = null;
    if (buffer && keepOpen) {
      lookbackSamples = buffer.beginNote((index, samples) => {
        writes.push(store.appendAudio(id, index, samples));
      });
    } else if (buffer) {
      lookback = buffer.lookback();
      lookbackSamples = lookback.length;
    }
    const rate = buffer?.sampleRate ?? NOTE_SAMPLE_RATE;
    const audioStartMs = Math.max(
      0,
      capturedMs - samplesToMs(lookbackSamples, rate),
    );
    let pinned: PinnedFrame;
    let seq: number;
    try {
      pinned = await pinning;
      seq = await store.takeSeq(session.id);
    } catch (error) {
      if (keepOpen) buffer?.endNote();
      throw error;
    }
    const base: StoredNote = {
      id,
      sessionId: session.id,
      workspaceId: session.workspaceId,
      notebookId: session.notebookId,
      seq,
      capturedMs,
      audioStartMs: buffer ? audioStartMs : null,
      audioEndMs: null,
      sampleRate: rate,
      image: null,
      audio: null,
      hasAudio: false,
      state: "open",
      endReason: null,
      createdAt: Date.now(),
    };
    const crop = session.crop;
    if (keepOpen) {
      // The note is open from now on; its image is stored in the background.
      const stored = (async () => {
        const image = await pinned.encode(crop);
        await store.putNote({ ...base, image });
      })();
      stored.catch(() => {});
      this.open = {
        id,
        capturedMs,
        audioStartMs,
        writes,
        stored,
        timer: setTimeout(() => {
          void this.serialized(async () => {
            if (this.open?.id === id) {
              await this.finishOpen("max_length");
              this.set({ notice: "max_length" });
            }
          });
        }, MAX_NOTE_MS),
      };
      this.set({ openSinceMs: capturedMs });
      return stored.then(
        (): PressResult => {
          void this.refreshCounts();
          return {
            outcome: "started",
            captured_ms: capturedMs,
            audio_start_ms: audioStartMs,
          };
        },
        async (): Promise<PressResult> => {
          // The image could not be stored: the note cannot exist.
          await this.serialized(async () => {
            if (this.open?.id !== id) return;
            if (this.open.timer) clearTimeout(this.open.timer);
            this.open = null;
            buffer?.endNote();
            this.set({ openSinceMs: null });
          });
          return { outcome: "error", reason: "storage" };
        },
      );
    }
    // One complete note per press ("Last 15 seconds only"), or no tab audio.
    const hasAudio = lookback !== null;
    return (async (): Promise<PressResult> => {
      const image = await pinned.encode(crop);
      await store.putNote({
        ...base,
        image,
        state: "queued",
        audio: lookback ? encodeWav([lookback], rate) : null,
        hasAudio,
        audioEndMs: hasAudio ? capturedMs : null,
      });
      this.set({ savedTick: this.status.savedTick + 1 });
      void this.refreshCounts();
      void flushStudyNotes(this.api);
      return {
        outcome: "saved",
        captured_ms: capturedMs,
        audio_start_ms: hasAudio ? audioStartMs : null,
        audio_end_ms: hasAudio ? capturedMs : null,
        has_audio: hasAudio,
      };
    })();
  }

  /** Complete the open note at the last sample we have. */
  private async finishOpen(reason: EndReason | null): Promise<PressResult> {
    const open = this.open;
    if (!open) return { outcome: "error", reason: "not_recording" };
    this.open = null;
    if (open.timer) clearTimeout(open.timer);
    const buffer = this.source?.buffer ?? null;
    // The audio ends here, whatever is still being written.
    const total = buffer?.endNote() ?? 0;
    await Promise.allSettled([open.stored, ...open.writes]);
    const stored = await store.getNote(open.id);
    const rate = stored?.sampleRate ?? NOTE_SAMPLE_RATE;
    const parts = await store.readAudio(open.id);
    const samples = parts.reduce((sum, part) => sum + part.length, 0) || total;
    const audioEndMs = open.audioStartMs + samplesToMs(samples, rate);
    if (stored) {
      await store.putNote({
        ...stored,
        state: "queued",
        audio: encodeWav(parts, rate),
        hasAudio: true,
        audioEndMs,
        endReason: reason,
      });
    }
    this.set({ openSinceMs: null, savedTick: this.status.savedTick + 1 });
    void this.refreshCounts();
    void flushStudyNotes(this.api);
    return {
      outcome: "saved",
      captured_ms: open.capturedMs,
      audio_start_ms: open.audioStartMs,
      audio_end_ms: audioEndMs,
      has_audio: true,
    };
  }

  /** The recording was paused: an open note ends, and nothing is kept until
   * it resumes (no audio from before the pause can reach a later note). */
  pause(): Promise<void> {
    return this.serialized(async () => {
      if (this.open) {
        await this.finishOpen("paused");
        this.set({ notice: "paused" });
      }
      this.source?.buffer?.setGate(false);
      this.source?.buffer?.reset();
    });
  }

  resume(): Promise<void> {
    return this.serialized(async () => {
      this.source?.buffer?.reset();
      this.source?.buffer?.setGate(true);
      if (this.status.notice === "paused") this.set({ notice: null });
    });
  }

  private async detach(reason: EndReason): Promise<void> {
    if (this.open) await this.finishOpen(reason);
    this.source?.close();
    this.source = null;
    if (this.session) {
      await store
        .updateSession(this.session.id, { ended: true })
        .catch(() => {});
    }
    this.session = null;
    flushListeners.delete(this.onFlush);
  }

  /** The recording stopped (or this page is going away): finish and let go. */
  end(reason: EndReason = "stopped"): Promise<void> {
    return this.serialized(async () => {
      if (!this.session) return;
      await this.detach(reason);
      this.set(IDLE_STUDY);
      void flushStudyNotes(this.api);
    });
  }
}
