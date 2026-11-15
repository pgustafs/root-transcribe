import "fake-indexeddb/auto";

import { beforeEach, describe, expect, it, vi } from "vitest";

import { parseEvents } from "@/lib/study/commandStream";
import {
  clampCrop,
  cropPixels,
  FULL_FRAME,
  isFullFrame,
} from "@/lib/study/frame";
import {
  type CaptureSource,
  countStudyNotes,
  discardStudySessions,
  linkStudySessions,
  recoverStudyNotes,
  StudyNoteController,
  type StudyStatus,
} from "@/lib/study/noteController";
import * as store from "@/lib/study/noteStore";
import { sourceKey, isHttpUrl } from "@/lib/study/sourceKey";
import { encodeWav, TabAudioBuffer } from "@/lib/study/tabAudio";

const RATE = 100; // 100 samples a second keeps the arithmetic readable

/** A ramp: sample n has the value n, so every sample is identifiable. */
function ramp(from: number, count: number): Float32Array {
  return Float32Array.from({ length: count }, (_, i) => from + i);
}

function joined(parts: Float32Array[]): number[] {
  return parts.flatMap((part) => Array.from(part));
}

describe("source identity", () => {
  it("reduces every YouTube link shape to one key", () => {
    for (const url of [
      "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=512s&list=PL1&si=x",
      "https://youtu.be/dQw4w9WgXcQ?t=90",
      "youtu.be/dQw4w9WgXcQ",
      "https://www.youtube.com/shorts/dQw4w9WgXcQ",
      "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    ])
      expect(sourceKey(url)).toBe("youtube:dQw4w9WgXcQ");
    expect(sourceKey("https://example.com/course")).toBeNull();
    expect(sourceKey("")).toBeNull();
    expect(isHttpUrl("example.com/a")).toBe(true);
    expect(isHttpUrl("javascript:alert(1)")).toBe(false);
  });
});

describe("tab audio buffer", () => {
  it("keeps at most 15 seconds and reports less early on", () => {
    const buffer = new TabAudioBuffer(RATE, 15, 5);
    buffer.append(ramp(0, 500)); // 5 seconds in
    expect(buffer.available()).toBe(500);
    expect(Array.from(buffer.lookback())).toEqual(Array.from(ramp(0, 500)));
    for (let from = 500; from < 4000; from += 130)
      buffer.append(ramp(from, 130));
    const last = buffer.lookback();
    expect(last.length).toBe(1500);
    expect(last[last.length - 1]).toBe(4009);
    expect(last[0]).toBe(4009 - 1499);
  });

  it("joins look-back and following audio without a gap or a repeat", () => {
    const buffer = new TabAudioBuffer(RATE, 15, 5);
    buffer.append(ramp(0, 2000));
    const chunks: Float32Array[] = [];
    const lookback = buffer.beginNote((index, samples) => {
      expect(index).toBe(chunks.length);
      chunks.push(samples);
    });
    expect(lookback).toBe(1500);
    for (let from = 2000; from < 3310; from += 131)
      buffer.append(ramp(from, 131));
    expect(chunks.length).toBeGreaterThan(1); // written while the note is open
    const total = buffer.endNote();
    const samples = joined(chunks);
    expect(samples.length).toBe(total);
    expect(samples[0]).toBe(500);
    // Consecutive values: nothing dropped, nothing doubled, through the join.
    samples.forEach((value, i) => expect(value).toBe(500 + i));
    expect(buffer.noteOpen).toBe(false);
  });

  it("never leaks audio from before a pause or a source change", () => {
    const buffer = new TabAudioBuffer(RATE, 15, 5);
    buffer.append(ramp(0, 900));
    buffer.setGate(false);
    buffer.reset();
    buffer.append(ramp(900, 300)); // while paused: ignored
    buffer.reset();
    buffer.setGate(true);
    expect(buffer.available()).toBe(0);
    buffer.append(ramp(5000, 200));
    expect(Array.from(buffer.lookback())).toEqual(Array.from(ramp(5000, 200)));
  });

  it("writes a WAV that states its own length and rate", async () => {
    const wav = encodeWav(
      [Float32Array.of(0, 1, -1), Float32Array.of(0.5)],
      16000,
    );
    const view = new DataView(await wav.arrayBuffer());
    const tag = (at: number) =>
      String.fromCharCode(...[0, 1, 2, 3].map((i) => view.getUint8(at + i)));
    expect([tag(0), tag(8), tag(36)]).toEqual(["RIFF", "WAVE", "data"]);
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(40, true)).toBe(8);
    expect(view.getInt16(46, true)).toBe(0x7fff);
    expect(view.getInt16(48, true)).toBe(-0x8000);
  });
});

describe("crop", () => {
  it("stays inside the frame and maps to whole pixels", () => {
    expect(isFullFrame(FULL_FRAME)).toBe(true);
    expect(clampCrop({ x: 0.9, y: -1, width: 0.5, height: 0.01 })).toEqual({
      x: 0.5,
      y: 0,
      width: 0.5,
      height: 0.05,
    });
    expect(
      cropPixels({ x: 0.25, y: 0.1, width: 0.5, height: 0.8 }, 2880, 1800),
    ).toEqual({ sx: 720, sy: 180, sw: 1440, sh: 1440 });
    expect(cropPixels(FULL_FRAME, 1280, 720)).toEqual({
      sx: 0,
      sy: 0,
      sw: 1280,
      sh: 720,
    });
  });
});

describe("command stream", () => {
  it("parses events across chunk boundaries and skips heartbeats", () => {
    const first = parseEvents(
      ': ping\n\nevent: hello\ndata: {"server_ms":5}\n\nevent: comm',
    );
    expect(first.events).toEqual([{ event: "hello", data: '{"server_ms":5}' }]);
    const second = parseEvents(
      `${first.rest}and\ndata: {"invocation":"a"}\n\n`,
    );
    expect(second.events).toEqual([
      { event: "command", data: '{"invocation":"a"}' },
    ]);
    expect(second.rest).toBe("");
  });
});

// --- the controller ----------------------------------------------------------

function fakeSource(buffer: TabAudioBuffer | null) {
  let lost: (() => void) | null = null;
  let live = true;
  let frames = 0;
  const source: CaptureSource = {
    buffer,
    hasVideo: true,
    isLive: () => live,
    grab: async () => {
      frames += 1;
      const taken = frames;
      return {
        encode: async () => new Blob([`frame-${taken}`], { type: "image/png" }),
      };
    },
    onLost: (listener) => {
      lost = listener;
    },
    close: () => {},
  };
  return {
    source,
    lose: () => {
      live = false;
      lost?.();
    },
  };
}

function fakeApi() {
  const calls: string[] = [];
  const notes = new Map<string, unknown>();
  return {
    calls,
    notes,
    api: {
      putSession: vi.fn(async (_w: string, id: string) => {
        calls.push(`session:${id}`);
        return {} as never;
      }),
      putNote: vi.fn(async (_w: string, id: string, note: unknown) => {
        calls.push(`note:${id}`);
        notes.set(id, note);
        return {} as never;
      }),
      linkSession: vi.fn(async (_w: string, id: string, recording: string) => {
        calls.push(`link:${id}:${recording}`);
      }),
      deleteSession: vi.fn(async (_w: string, id: string) => {
        calls.push(`delete:${id}`);
      }),
      acknowledgeCommand: vi.fn(async () => {}),
    },
  };
}

async function setup(mode: store.StudyMode, withAudio = true) {
  const buffer = withAudio ? new TabAudioBuffer(RATE, 15, 5) : null;
  const { source, lose } = fakeSource(buffer);
  const backend = fakeApi();
  let clock = 0;
  let paused = false;
  let status: StudyStatus | null = null;
  let ids = 0;
  let onCommand: ((command: never, age: number) => void) | null = null;
  const controller = new StudyNoteController({
    clock: () => clock,
    isPaused: () => paused,
    onChange: (next) => {
      status = next;
    },
    createSource: async () => source,
    openStream: (_id, handlers) => {
      onCommand = handlers.onCommand as never;
      handlers.onState?.(true);
      return () => {};
    },
    api: backend.api,
    newId: () => `id-${(ids += 1)}`,
  });
  await controller.begin(
    {
      workspaceId: "ws",
      notebookId: "nb",
      notebookTitle: "Query plans",
      language: "sv",
      mode,
      localRecordingId: "local-1",
    },
    {} as MediaStream,
  );
  await vi.waitFor(() => expect(onCommand).not.toBeNull());
  return {
    controller,
    buffer,
    backend,
    lose,
    setClock: (ms: number) => {
      clock = ms;
    },
    setPaused: (value: boolean) => {
      paused = value;
    },
    status: () => status as unknown as StudyStatus,
    press: async (invocation: string, age = 20) => {
      onCommand?.(
        {
          invocation,
          action: "toggle",
          sent_ms: 0,
          stale_after_ms: 2000,
        } as never,
        age,
      );
      await vi.waitFor(async () =>
        expect(await store.getInvocation(invocation)).toBeDefined(),
      );
      return (await store.getInvocation(invocation)) as store.InvocationResult;
    },
  };
}

async function wipe() {
  await store.closeStudyStore();
  await new Promise<void>((resolve) => {
    const request = indexedDB.deleteDatabase("study-notes");
    request.onsuccess = request.onerror = request.onblocked = () => resolve();
  });
}

describe("study note controller", () => {
  beforeEach(wipe);

  it("starts a note with the look-back and finishes it on the next press", async () => {
    const t = await setup("until_stopped");
    t.buffer?.append(ramp(0, 2700)); // 27 s of tab audio so far
    t.setClock(522_000);
    const started = await t.press("press-1");
    expect(started).toMatchObject({
      outcome: "started",
      captured_ms: 522_000,
      audio_start_ms: 507_000,
    });
    expect(t.status().openSinceMs).toBe(522_000);
    // The same press again changes nothing.
    expect(await t.press("press-1")).toEqual(started);
    expect(t.status().openSinceMs).toBe(522_000);

    t.buffer?.append(ramp(2700, 2100)); // 21 more seconds
    const saved = await t.press("press-2");
    expect(saved).toMatchObject({
      outcome: "saved",
      audio_start_ms: 507_000,
      audio_end_ms: 543_000,
    });
    expect(await t.press("press-2")).toEqual(saved);
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(1));
    const sent = [...t.backend.notes.values()][0] as {
      seq: number;
      capturedMs: number;
      audioStartMs: number;
      audioEndMs: number;
    };
    expect(sent.seq).toBe(0);
    expect(sent.capturedMs).toBe(522_000);
    // 15 s of look-back plus the 21 s that followed, by sample count.
    expect(sent.audioEndMs - sent.audioStartMs).toBe(36_000);
    expect(t.backend.calls.filter((c) => c.startsWith("note:"))).toHaveLength(
      1,
    );
    await vi.waitFor(() => expect(t.status().count).toBe(1));
    expect(t.backend.api.acknowledgeCommand).toHaveBeenCalledTimes(4);
  });

  it("takes one complete note per press in last-15-seconds mode", async () => {
    const t = await setup("last_15s");
    t.buffer?.append(ramp(0, 500)); // only 5 seconds exist yet
    t.setClock(5_000);
    const first = await t.press("a");
    expect(first).toMatchObject({
      outcome: "saved",
      audio_start_ms: 0,
      audio_end_ms: 5_000,
    });
    t.buffer?.append(ramp(500, 50));
    t.setClock(5_500);
    const second = await t.press("b");
    expect(second).toMatchObject({ outcome: "saved", audio_start_ms: 0 });
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(2));
    const seqs = [...t.backend.notes.values()].map(
      (n) => (n as { seq: number }).seq,
    );
    expect(seqs.sort()).toEqual([0, 1]);
    expect(t.status().openSinceMs).toBeNull();
  });

  it("drops a press that arrives too late, and refuses while paused", async () => {
    const t = await setup("last_15s");
    expect(await t.press("late", 3000)).toEqual({
      outcome: "error",
      reason: "stale",
    });
    t.setPaused(true);
    await t.controller.pause();
    expect(await t.press("paused")).toEqual({
      outcome: "error",
      reason: "paused",
    });
    expect(t.backend.notes.size).toBe(0);
    // After resuming, nothing from before the pause is in the note.
    t.buffer?.append(ramp(0, 400));
    t.setPaused(false);
    await t.controller.resume();
    t.buffer?.append(ramp(9000, 100));
    t.setClock(60_000);
    const after = await t.press("resumed");
    expect(after).toMatchObject({
      audio_start_ms: 59_000,
      audio_end_ms: 60_000,
    });
  });

  it("ends an open note when the recording pauses or sharing is lost", async () => {
    const t = await setup("until_stopped");
    t.buffer?.append(ramp(0, 300));
    t.setClock(3_000);
    await t.controller.toggle();
    await t.controller.pause();
    expect(t.status().openSinceMs).toBeNull();
    expect(t.status().notice).toBe("paused");
    await t.controller.resume();
    t.buffer?.append(ramp(0, 100));
    t.setClock(10_000);
    await t.controller.toggle();
    t.lose();
    await vi.waitFor(() => expect(t.status().videoAvailable).toBe(false));
    expect(t.status().openSinceMs).toBeNull();
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(2));
    const reasons = [...t.backend.notes.values()].map(
      (n) => (n as { endReason: string }).endReason,
    );
    expect(reasons.sort()).toEqual(["paused", "sharing_lost"]);
    // No frame can be taken any more: the press says why.
    expect(await t.press("gone")).toEqual({
      outcome: "error",
      reason: "sharing_lost",
    });
  });

  it("saves a screenshot only when the tab has no audio", async () => {
    const t = await setup("until_stopped", false);
    t.setClock(8_000);
    const result = await t.controller.toggle();
    expect(result).toMatchObject({ outcome: "saved", has_audio: false });
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(1));
    expect(
      ([...t.backend.notes.values()][0] as { audio: Blob | null }).audio,
    ).toBeNull();
    expect(t.status().audioAvailable).toBe(false);
  });

  it("links, recovers and discards by the recording the notes belong to", async () => {
    const t = await setup("until_stopped");
    t.buffer?.append(ramp(0, 200));
    t.setClock(2_000);
    await t.controller.toggle(); // left open, as if the page went away
    expect(await countStudyNotes("local-1")).toBe(1);
    await recoverStudyNotes(t.backend.api);
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(1));
    const recovered = [...t.backend.notes.values()][0] as {
      endReason: string;
      audioEndMs: number;
    };
    expect(recovered.endReason).toBe("interrupted");
    expect(recovered.audioEndMs).toBe(2_000);
    await linkStudySessions("local-1", "rec-9", t.backend.api);
    expect(t.backend.calls.some((c) => c.endsWith(":rec-9"))).toBe(true);
    // Everything is on the server and linked: nothing is left behind locally.
    expect(await store.listSessions()).toEqual([]);

    const again = await setup("last_15s");
    again.buffer?.append(ramp(0, 100));
    await again.controller.toggle();
    await discardStudySessions("local-1", again.backend.api);
    expect(again.backend.calls.some((c) => c.startsWith("delete:"))).toBe(true);
    expect(await countStudyNotes("local-1")).toBe(0);
  });
});
