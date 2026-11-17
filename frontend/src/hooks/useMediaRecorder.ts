/**
 * Audio capture via the MediaRecorder API: the microphone, another browser
 * tab (Chrome/Edge), or both. Prefers Opus in a WebM container. The server
 * converts to WAV for STT/diarization.
 *
 * Recording both (an online meeting) keeps them apart: the microphone on the
 * left channel, the tab on the right. Who is here and who is in the call is
 * then known from the channel instead of guessed from the voices.
 */

import { useCallback, useRef, useState } from "react";

export type RecorderStatus = "idle" | "recording" | "paused";

const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

function pickMimeType(): string {
  if (typeof MediaRecorder === "undefined") return "audio/webm";
  return (
    MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported(m)) ??
    "audio/webm"
  );
}

export type RecordingResult = { blob: Blob; mimeType: string };

/** Per-timeslice metadata for privacy-safe assembly diagnostics (issue #37) —
 * timing only, never audio content. */
export type ChunkMeta = {
  /** Wall-clock ms when the timeslice landed. */
  recordedAt: number;
  /** MediaRecorder's own timecode, when the browser provides one. */
  timecode: number | null;
};

/** What is recorded: the people in this room, an online meeting (this room's
 * microphone and the meeting's tab, on separate channels), or a tab alone
 * (the microphone is never opened). */
export type RecordingSource = "mic" | "mic_tab" | "tab";

export type StartOptions = {
  source?: RecordingSource;
  /** Specific input device (from listAudioInputs); default mic otherwise. */
  deviceId?: string;
  /** Receives every timeslice as it lands (IndexedDB persistence, issue #6). */
  onChunk?: (seq: number, chunk: Blob, meta: ChunkMeta) => void;
  /**
   * The container the recorder actually chose, reported as soon as it starts
   * (issue #37 review): callers persist it immediately, so a crash BEFORE stop
   * still knows whether the local bytes are WebM, Ogg or MP4 — otherwise
   * recovery would preflight valid Ogg/MP4 against the WebM magic and refuse it.
   */
  onStart?: (mimeType: string) => void;
  /**
   * Keep this page in front after the tab is picked (study notes: the crop
   * step is shown next). Chrome otherwise jumps to the shared tab.
   */
  keepFocus?: boolean;
};

type FocusController = { setFocusBehavior: (behavior: string) => void };
declare const CaptureController: { new (): FocusController } | undefined;

/** Ask for a tab/window with its audio. */
async function requestDisplay(keepFocus: boolean): Promise<MediaStream> {
  const options: DisplayMediaStreamOptions & { controller?: FocusController } =
    {
      video: true,
      audio: true,
    };
  let controller: FocusController | null = null;
  if (keepFocus && typeof CaptureController !== "undefined") {
    controller = new CaptureController();
    options.controller = controller;
  }
  const display = await navigator.mediaDevices.getDisplayMedia(options);
  try {
    // Only valid right after the picker closes, and only for a tab or window.
    controller?.setFocusBehavior("focus-capturing-application");
  } catch {
    // A whole screen was shared, or the browser has no say: nothing to do.
  }
  return display;
}

/** Available microphones. Labels are empty until mic permission is granted —
 * callers show a generic name for those. */
export async function listAudioInputs(): Promise<
  { deviceId: string; label: string }[]
> {
  if (!isRecordingSupported() || !navigator.mediaDevices.enumerateDevices) {
    return [];
  }
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices
      .filter((d) => d.kind === "audioinput")
      .map((d) => ({ deviceId: d.deviceId, label: d.label }));
  } catch {
    return [];
  }
}

/**
 * True only in a secure context (HTTPS or http://localhost). Outside one,
 * `navigator.mediaDevices` is undefined and recording is impossible — callers
 * should surface an explanation rather than let getUserMedia throw.
 */
export function isRecordingSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === "function"
  );
}

export function useMediaRecorder() {
  const [status, setStatus] = useState<RecorderStatus>("idle");
  const [durationMs, setDurationMs] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // Live input level 0..1 for the meter (issue #6, B5).
  const [level, setLevel] = useState(0);
  // Whether the shared tab/window actually delivered an audio track (null =
  // tab audio not requested) — silently recording mic-only was a #6 finding.
  const [tabAudioPresent, setTabAudioPresent] = useState<boolean | null>(null);
  // "mic-ended" | "tab-ended" when a required track died mid-recording.
  const [trackWarning, setTrackWarning] = useState<string | null>(null);
  const isSupported = isRecordingSupported();

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamsRef = useRef<MediaStream[]>([]);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const timerRef = useRef<number | null>(null);
  const meterRef = useRef<number | null>(null);
  const startedAtRef = useRef(0);
  // Elapsed time at the moment of pausing; null while running.
  const pausedAtRef = useRef<number | null>(null);
  const mimeRef = useRef("audio/webm");
  // The shared tab's own stream and where its audio enters the mix. Study
  // notes read the original tracks; "Change source" swaps them.
  const displayRef = useRef<MediaStream | null>(null);
  const tabSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  // Where the tab's audio goes in: the node and which of its inputs (the
  // right channel of the microphone/tab pair, or the recording itself).
  const mixRef = useRef<AudioNode | null>(null);
  const tabInputRef = useRef(0);
  const micRef = useRef<MediaStream | null>(null);

  const stopTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const cleanupStreams = useCallback(() => {
    if (meterRef.current !== null) {
      window.clearInterval(meterRef.current);
      meterRef.current = null;
    }
    setLevel(0);
    streamsRef.current.forEach((s) => s.getTracks().forEach((t) => t.stop()));
    streamsRef.current = [];
    audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
    displayRef.current = null;
    tabSourceRef.current = null;
    mixRef.current = null;
    micRef.current = null;
  }, []);

  /** Wire a shared tab into the recording and watch its tracks. */
  const attachDisplay = useCallback(
    (display: MediaStream, ctx: AudioContext, dest: AudioNode) => {
      displayRef.current = display;
      const hasTabAudio = display.getAudioTracks().length > 0;
      setTabAudioPresent(hasTabAudio);
      tabSourceRef.current = null;
      if (hasTabAudio) {
        const source = ctx.createMediaStreamSource(display);
        source.connect(dest, 0, tabInputRef.current);
        tabSourceRef.current = source;
      }
      // Any of its tracks ending means the sharing stopped.
      display.getTracks().forEach((track) => {
        track.onended = () => setTrackWarning("tab-ended");
      });
    },
    [],
  );

  const start = useCallback(
    // Returns true when recording is live (callers branch on it immediately —
    // reading the `error` state after the await would be a stale closure).
    async ({
      source = "mic",
      deviceId,
      onChunk,
      onStart,
      keepFocus = false,
    }: StartOptions = {}): Promise<boolean> => {
      setError(null);
      setTrackWarning(null);
      setTabAudioPresent(null);
      if (!isRecordingSupported()) {
        // Secure-context guard: mediaDevices is undefined on plain-HTTP origins.
        setError("insecure-context");
        return false;
      }
      try {
        streamsRef.current = [];
        let mic: MediaStream | null = null;
        if (source !== "tab") {
          // A tab alone needs no microphone: it is never asked for.
          mic = await navigator.mediaDevices.getUserMedia({
            audio: {
              echoCancellation: true,
              noiseSuppression: true,
              ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
            },
          });
          micRef.current = mic;
          streamsRef.current.push(mic);
          // A required track dying mid-meeting (device unplugged, share
          // stopped) must warn immediately, not silently record silence
          // (issue #6).
          mic.getAudioTracks().forEach((track) => {
            track.onended = () => setTrackWarning("mic-ended");
          });
        }
        let recordStream: MediaStream;
        if (mic && source === "mic") {
          recordStream = mic;
        } else {
          const display = await requestDisplay(keepFocus);
          streamsRef.current.push(display);
          const ctx = new AudioContext();
          audioCtxRef.current = ctx;
          const dest = ctx.createMediaStreamDestination();
          if (mic) {
            // An online meeting: the microphone on the left channel, the tab
            // on the right. Each input of the merger is one channel.
            const pair = ctx.createChannelMerger(2);
            ctx.createMediaStreamSource(mic).connect(pair, 0, 0);
            pair.connect(dest);
            mixRef.current = pair;
            tabInputRef.current = 1;
          } else {
            mixRef.current = dest;
            tabInputRef.current = 0;
          }
          attachDisplay(display, ctx, mixRef.current);
          if (!mic && display.getAudioTracks().length === 0) {
            // A tab alone, shared without its audio: there is nothing to
            // record. Say so instead of saving silence.
            throw new Error("tab-no-audio");
          }
          recordStream = dest.stream;
        }

        // Input level meter: RMS of the recorded stream, polled for the bar.
        try {
          const ctx = audioCtxRef.current ?? new AudioContext();
          audioCtxRef.current = ctx;
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 512;
          ctx.createMediaStreamSource(recordStream).connect(analyser);
          const samples = new Uint8Array(analyser.fftSize);
          meterRef.current = window.setInterval(() => {
            analyser.getByteTimeDomainData(samples);
            let sum = 0;
            for (const value of samples) {
              const centered = (value - 128) / 128;
              sum += centered * centered;
            }
            setLevel(Math.min(1, Math.sqrt(sum / samples.length) * 3));
          }, 150);
        } catch {
          // No meter (e.g. AudioContext unavailable) — recording still works.
        }

        const mimeType = pickMimeType();
        mimeRef.current = mimeType;
        const recorder = new MediaRecorder(recordStream, {
          mimeType,
          audioBitsPerSecond: 128000,
        });
        chunksRef.current = [];
        let seq = 0;
        recorder.ondataavailable = (e) => {
          if (e.data.size > 0) {
            chunksRef.current.push(e.data);
            onChunk?.(seq, e.data, {
              recordedAt: Date.now(),
              timecode: Number.isFinite(e.timecode) ? e.timecode : null,
            });
            seq += 1;
          }
        };
        recorder.start(1000); // 1s timeslices for resilience
        // The container the recorder actually settled on (it may differ from
        // the requested one) — reported before any chunk lands.
        mimeRef.current = recorder.mimeType || mimeType;
        onStart?.(mimeRef.current);
        recorderRef.current = recorder;
        startedAtRef.current = Date.now();
        pausedAtRef.current = null;
        setDurationMs(0);
        setStatus("recording");
        timerRef.current = window.setInterval(
          () => setDurationMs(Date.now() - startedAtRef.current),
          250,
        );
        return true;
      } catch (e) {
        setError(
          e instanceof Error ? e.message : "Could not access the microphone",
        );
        cleanupStreams();
        setStatus("idle");
        return false;
      }
    },
    [cleanupStreams, attachDisplay],
  );

  /** The shared tab's stream (its original video and audio tracks). */
  const getDisplayStream = useCallback(() => displayRef.current, []);

  /** The microphone's own stream (notes in a recording without a shared tab);
   * null when only a tab is recorded. */
  const getMicStream = useCallback(() => micRef.current, []);

  /**
   * Share another tab mid-recording ("Change source"). The meeting recording
   * continues; the new tab's audio replaces the old one in the mix. Returns
   * the new stream, or null when the picker was dismissed (nothing changes).
   */
  const replaceDisplay = useCallback(
    async (keepFocus = false): Promise<MediaStream | null> => {
      const ctx = audioCtxRef.current;
      const dest = mixRef.current;
      if (!ctx || !dest) return null;
      let display: MediaStream;
      try {
        display = await requestDisplay(keepFocus);
      } catch {
        return null;
      }
      const previous = displayRef.current;
      tabSourceRef.current?.disconnect();
      if (previous) {
        // stop() does not fire "ended": no false "sharing stopped" warning.
        previous.getTracks().forEach((track) => {
          track.onended = null;
          track.stop();
        });
        streamsRef.current = streamsRef.current.filter((s) => s !== previous);
      }
      streamsRef.current.push(display);
      setTrackWarning((current) => (current === "tab-ended" ? null : current));
      attachDisplay(display, ctx, dest);
      return display;
    },
    [attachDisplay],
  );

  /** Elapsed recording time right now, to the millisecond (the displayed
   * `durationMs` only ticks four times a second). Frozen while paused. */
  const getElapsedMs = useCallback((): number => {
    if (!recorderRef.current) return 0;
    return pausedAtRef.current ?? Date.now() - startedAtRef.current;
  }, []);
  const isPaused = useCallback(() => pausedAtRef.current !== null, []);

  const pause = useCallback(() => {
    recorderRef.current?.pause();
    pausedAtRef.current = Date.now() - startedAtRef.current;
    stopTimer();
    setStatus("paused");
  }, [stopTimer]);

  const resume = useCallback(() => {
    recorderRef.current?.resume();
    const elapsed = pausedAtRef.current ?? durationMs;
    pausedAtRef.current = null;
    startedAtRef.current = Date.now() - elapsed;
    timerRef.current = window.setInterval(
      () => setDurationMs(Date.now() - startedAtRef.current),
      250,
    );
    setStatus("recording");
  }, [durationMs]);

  const stop = useCallback((): Promise<RecordingResult> => {
    return new Promise((resolve) => {
      const recorder = recorderRef.current;
      stopTimer();
      if (!recorder) {
        resolve({ blob: new Blob(), mimeType: mimeRef.current });
        return;
      }
      recorder.onstop = async () => {
        const blob = new Blob(chunksRef.current, { type: mimeRef.current });
        // Force the bytes to materialize before we drop the chunk refs.
        await blob.arrayBuffer();
        cleanupStreams();
        recorderRef.current = null;
        setStatus("idle");
        resolve({ blob, mimeType: mimeRef.current });
      };
      recorder.stop();
    });
  }, [cleanupStreams, stopTimer]);

  const cancel = useCallback(() => {
    stopTimer();
    try {
      recorderRef.current?.stop();
    } catch {
      /* ignore */
    }
    cleanupStreams();
    recorderRef.current = null;
    chunksRef.current = [];
    setStatus("idle");
    setDurationMs(0);
  }, [cleanupStreams, stopTimer]);

  return {
    status,
    durationMs,
    error,
    level,
    tabAudioPresent,
    trackWarning,
    isSupported,
    getDisplayStream,
    getMicStream,
    replaceDisplay,
    getElapsedMs,
    isPaused,
    start,
    pause,
    resume,
    stop,
    cancel,
  };
}
