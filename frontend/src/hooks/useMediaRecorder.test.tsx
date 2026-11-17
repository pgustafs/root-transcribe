import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { useMediaRecorder } from "./useMediaRecorder";

/** A stream with the given kinds of track ("audio", "video"). */
function stream(...kinds: string[]) {
  const tracks = kinds.map((kind) => ({ kind, stop: vi.fn(), onended: null }));
  return {
    getTracks: () => tracks,
    getAudioTracks: () => tracks.filter((t) => t.kind === "audio"),
  } as unknown as MediaStream;
}

type Wire = { from: unknown; to: string; input: number | undefined };
let wires: Wire[];
let getUserMedia: ReturnType<typeof vi.fn>;
let getDisplayMedia: ReturnType<typeof vi.fn>;
let recorded: MediaStream[];
let mic: MediaStream;

beforeEach(() => {
  wires = [];
  recorded = [];
  mic = stream("audio");
  getUserMedia = vi.fn(async () => mic);
  getDisplayMedia = vi.fn(async () => stream("video", "audio"));
  vi.stubGlobal("navigator", {
    mediaDevices: { getUserMedia, getDisplayMedia },
  });
  const node = (name: string) => ({
    name,
    connect: (to: { name: string }, _output?: number, input?: number) =>
      wires.push({ from: name, to: to.name, input }),
    disconnect: vi.fn(),
  });
  vi.stubGlobal(
    "AudioContext",
    class {
      close = vi.fn(async () => {});
      createMediaStreamDestination = () => ({
        ...node("recording"),
        stream: stream("audio"),
      });
      createChannelMerger = () => node("pair");
      createMediaStreamSource = (s: MediaStream) =>
        node(s === mic ? "microphone" : "tab");
      createAnalyser = () => ({
        ...node("meter"),
        fftSize: 0,
        getByteTimeDomainData: vi.fn(),
      });
    },
  );
  vi.stubGlobal(
    "MediaRecorder",
    class {
      static isTypeSupported = () => true;
      mimeType = "audio/webm;codecs=opus";
      state = "inactive";
      ondataavailable = null;
      constructor(s: MediaStream) {
        recorded.push(s);
      }
      start = vi.fn();
      stop = vi.fn();
    },
  );
});

afterEach(() => vi.unstubAllGlobals());

/** The wiring that carries sound into the recording (the meter aside). */
const sound = () => wires.filter((w) => w.to !== "meter");

it("keeps an online meeting's microphone and tab on separate channels", async () => {
  const { result } = renderHook(() => useMediaRecorder());
  await act(async () => {
    expect(await result.current.start({ source: "mic_tab" })).toBe(true);
  });
  expect(getUserMedia).toHaveBeenCalledTimes(1);
  expect(getDisplayMedia).toHaveBeenCalledTimes(1);
  // Microphone into the pair's first input (left), tab into its second
  // (right); the pair is what gets recorded.
  expect(sound()).toEqual([
    { from: "microphone", to: "pair", input: 0 },
    { from: "pair", to: "recording", input: undefined },
    { from: "tab", to: "pair", input: 1 },
  ]);
  expect(result.current.tabAudioPresent).toBe(true);
});

it("records a tab alone without ever asking for the microphone", async () => {
  const { result } = renderHook(() => useMediaRecorder());
  await act(async () => {
    expect(await result.current.start({ source: "tab" })).toBe(true);
  });
  expect(getUserMedia).not.toHaveBeenCalled();
  expect(sound()).toEqual([{ from: "tab", to: "recording", input: 0 }]);
  expect(result.current.getMicStream()).toBeNull();
});

it("says so when a tab alone is shared without its sound", async () => {
  getDisplayMedia.mockResolvedValue(stream("video"));
  const { result } = renderHook(() => useMediaRecorder());
  await act(async () => {
    expect(await result.current.start({ source: "tab" })).toBe(false);
  });
  expect(result.current.error).toBe("tab-no-audio");
  expect(recorded).toHaveLength(0);
});

it("records the room from the microphone as it is", async () => {
  const { result } = renderHook(() => useMediaRecorder());
  await act(async () => {
    expect(await result.current.start({ source: "mic" })).toBe(true);
  });
  expect(getDisplayMedia).not.toHaveBeenCalled();
  expect(sound()).toEqual([]);
  expect(recorded).toEqual([mic]);
  expect(result.current.getMicStream()).toBe(mic);
});
