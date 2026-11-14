/**
 * Audio capture via the MediaRecorder API, with optional tab/system-audio
 * mixing (record another browser tab + your mic together, Chrome/Edge). Prefers
 * Opus in a WebM container. The server converts to WAV for STT/diarization.
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

export type StartOptions = {
  includeTabAudio?: boolean;
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
};

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
  const mimeRef = useRef("audio/webm");

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
  }, []);

  const start = useCallback(
    // Returns true when recording is live (callers branch on it immediately —
    // reading the `error` state after the await would be a stale closure).
    async ({
      includeTabAudio = false,
      deviceId,
      onChunk,
      onStart,
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
        const mic = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
          },
        });
        streamsRef.current = [mic];
        let recordStream: MediaStream = mic;
        // A required track dying mid-meeting (device unplugged, share stopped)
        // must warn immediately, not silently record silence (issue #6).
        mic.getAudioTracks().forEach((track) => {
          track.onended = () => setTrackWarning("mic-ended");
        });

        if (includeTabAudio) {
          // Capture another tab/window/screen and mix its audio with the mic.
          const display = await navigator.mediaDevices.getDisplayMedia({
            video: true,
            audio: true,
          });
          streamsRef.current.push(display);
          const ctx = new AudioContext();
          audioCtxRef.current = ctx;
          const dest = ctx.createMediaStreamDestination();
          ctx.createMediaStreamSource(mic).connect(dest);
          const hasTabAudio = display.getAudioTracks().length > 0;
          setTabAudioPresent(hasTabAudio);
          if (hasTabAudio) {
            ctx.createMediaStreamSource(display).connect(dest);
            display.getAudioTracks().forEach((track) => {
              track.onended = () => setTrackWarning("tab-ended");
            });
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
    [cleanupStreams],
  );

  const pause = useCallback(() => {
    recorderRef.current?.pause();
    stopTimer();
    setStatus("paused");
  }, [stopTimer]);

  const resume = useCallback(() => {
    recorderRef.current?.resume();
    const elapsed = durationMs;
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
    start,
    pause,
    resume,
    stop,
    cancel,
  };
}
