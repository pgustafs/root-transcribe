import "fake-indexeddb/auto";

import { beforeEach, describe, expect, it, vi } from "vitest";

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
import { difference, SlideDetector } from "@/lib/study/slideWatch";
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

// --- the controller ----------------------------------------------------------

function fakeSource(buffer: TabAudioBuffer | null, hasVideo = true) {
  let lost: (() => void) | null = null;
  let live = true;
  let frames = 0;
  // What the shared tab shows: every cell of the small grey picture has this
  // value, so "a new slide" is simply another number.
  let picture = 10;
  let released = 0;
  const encoded: number[] = [];
  const source: CaptureSource = {
    buffer,
    hasVideo,
    isLive: () => live,
    grab: async () => {
      if (!hasVideo) throw new Error("No video is shared");
      frames += 1;
      const taken = frames;
      const shown = picture;
      return {
        encode: async () => {
          encoded.push(shown);
          return new Blob([`frame-${taken}`], { type: "image/png" });
        },
        signature: () => new Uint8Array(576).fill(shown),
        release: () => {
          released += 1;
        },
      };
    },
    onTick: () => {},
    onLost: (listener) => {
      lost = listener;
    },
    close: () => {},
  };
  return {
    source,
    show: (value: number) => {
      picture = value;
    },
    released: () => released,
    encoded,
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
    },
  };
}

async function setup(
  mode: store.StudyMode,
  withAudio = true,
  offerSlides = false,
  audioOnly = false,
) {
  const buffer = withAudio ? new TabAudioBuffer(RATE, 15, 5) : null;
  const { source, lose, show, released, encoded } = fakeSource(
    buffer,
    !audioOnly,
  );
  const backend = fakeApi();
  let clock = 0;
  let paused = false;
  let status: StudyStatus | null = null;
  let ids = 0;
  const controller = new StudyNoteController({
    clock: () => clock,
    isPaused: () => paused,
    onChange: (next) => {
      status = next;
    },
    createSource: async () => source,
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
      offerSlides,
      audioOnly,
    },
    {} as MediaStream,
  );
  return {
    controller,
    buffer,
    backend,
    lose,
    show,
    released,
    encoded,
    setClock: (ms: number) => {
      clock = ms;
    },
    setPaused: (value: boolean) => {
      paused = value;
    },
    status: () => status as unknown as StudyStatus,
    press: () => controller.toggle(),
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
    const started = await t.press();
    expect(started).toMatchObject({
      outcome: "started",
      captured_ms: 522_000,
      audio_start_ms: 507_000,
    });
    expect(t.status().openSinceMs).toBe(522_000);

    t.buffer?.append(ramp(2700, 2100)); // 21 more seconds
    const saved = await t.press();
    expect(saved).toMatchObject({
      outcome: "saved",
      audio_start_ms: 507_000,
      audio_end_ms: 543_000,
    });
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
  });

  it("takes one complete note per press in last-15-seconds mode", async () => {
    const t = await setup("last_15s");
    t.buffer?.append(ramp(0, 500)); // only 5 seconds exist yet
    t.setClock(5_000);
    const first = await t.press();
    expect(first).toMatchObject({
      outcome: "saved",
      audio_start_ms: 0,
      audio_end_ms: 5_000,
    });
    t.buffer?.append(ramp(500, 50));
    t.setClock(5_500);
    const second = await t.press();
    expect(second).toMatchObject({ outcome: "saved", audio_start_ms: 0 });
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(2));
    const seqs = [...t.backend.notes.values()].map(
      (n) => (n as { seq: number }).seq,
    );
    expect(seqs.sort()).toEqual([0, 1]);
    expect(t.status().openSinceMs).toBeNull();
  });

  it("refuses a press while paused and keeps earlier audio out afterwards", async () => {
    const t = await setup("last_15s");
    t.setPaused(true);
    await t.controller.pause();
    expect(await t.press()).toEqual({
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
    const after = await t.press();
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
    expect(await t.press()).toEqual({
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

  it("carries a mark and a thought with the note", async () => {
    const t = await setup("until_stopped");
    t.buffer?.append(ramp(0, 300));
    t.setClock(3_000);
    // A mark with no note open takes a note that carries it.
    await t.controller.mark("unclear", { thought: "Why not a full scan?" });
    expect(t.status().openSinceMs).toBe(3_000);
    expect(t.status().openTags).toEqual(["unclear"]);
    // On the open note marks switch on and off, and the note stays open.
    await t.controller.mark("important");
    await t.controller.mark("unclear");
    await t.controller.addThought("Compare the plans.");
    expect(t.status().openTags).toEqual(["important"]);
    expect(t.status().openSinceMs).toBe(3_000);
    await t.controller.toggle({ thought: "Ask on Friday." });
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(1));
    expect([...t.backend.notes.values()][0]).toMatchObject({
      tags: ["important"],
      comment: "Why not a full scan?\nCompare the plans.\nAsk on Friday.",
    });

    // "Last 15 seconds": every mark or thought is one complete note.
    const quick = await setup("last_15s");
    quick.buffer?.append(ramp(0, 100));
    await quick.controller.mark("important");
    await quick.controller.addThought("Remember this.");
    await vi.waitFor(() => expect(quick.backend.notes.size).toBe(2));
    const sent = [...quick.backend.notes.values()] as {
      tags: string[];
      comment: string | null;
    }[];
    expect(sent.map((note) => note.tags)).toEqual([["important"], []]);
    expect(sent.map((note) => note.comment)).toEqual([null, "Remember this."]);
  });

  it("offers the slide that went away, with the talk before the change", async () => {
    const t = await setup("last_15s", true, true);
    // Slide A is showing while 12 seconds are spoken.
    t.buffer?.append(ramp(0, 1200));
    t.setClock(12_000);
    await t.controller.sample();
    await t.controller.sample();
    expect(t.status().offer).toBeNull();
    // Slide B replaces it. The first look sees the change, the second sees
    // that the new picture is still.
    t.show(200);
    t.setClock(14_000);
    await t.controller.sample();
    expect(t.status().offer).toBeNull();
    t.buffer?.append(ramp(5000, 200)); // talk about B, not part of A's note
    t.setClock(16_000);
    await t.controller.sample();
    expect(t.status().offer).toMatchObject({ capturedMs: 12_000 });

    const kept = await t.controller.keepOffer({ thought: "Good summary." });
    // The audio ends where slide A stopped showing, not where it was kept.
    expect(kept).toMatchObject({
      outcome: "saved",
      captured_ms: 12_000,
      audio_start_ms: 2_000,
      audio_end_ms: 14_000,
    });
    expect(t.status().offer).toBeNull();
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(1));
    const sent = [...t.backend.notes.values()][0] as { comment: string };
    expect(sent.comment).toBe("Good summary.");
    // The image is slide A (picture 10), not what shows now (200).
    expect(t.encoded).toEqual([10]);
    expect((await t.controller.keepOffer()).reason).toBe("offer_gone");
  });

  it("makes no offer for moving video or while paused", async () => {
    const t = await setup("last_15s", true, true);
    // Every look shows something else: video, not slides.
    for (const value of [10, 90, 170, 250, 40, 120]) {
      t.show(value);
      await t.controller.sample();
    }
    expect(t.status().offer).toBeNull();
    // A slide, a pause, another slide afterwards: nothing to offer, because
    // nobody saw the change happen.
    t.show(10);
    await t.controller.sample();
    await t.controller.sample();
    await t.controller.pause();
    await t.controller.resume();
    t.show(200);
    await t.controller.sample();
    await t.controller.sample();
    expect(t.status().offer).toBeNull();
    // Frames that were looked at and not kept are let go.
    expect(t.released()).toBeGreaterThan(5);
  });

  it("keeps a minute for a question, as a note of its own", async () => {
    const t = await setup("until_stopped");
    t.buffer?.append(ramp(0, 9000)); // 90 seconds of talk
    t.setClock(90_000);
    // A note is open; the question does not end or change it.
    await t.controller.toggle();
    expect(t.status().openSinceMs).toBe(90_000);
    t.buffer?.append(ramp(9000, 500));
    t.setClock(95_000);
    const asked = await t.controller.question({ thought: "From Anna" });
    expect(asked).toMatchObject({
      outcome: "saved",
      captured_ms: 95_000,
      // One minute back, not the usual 15 seconds.
      audio_start_ms: 35_000,
      audio_end_ms: 95_000,
    });
    expect(t.status().openSinceMs).toBe(90_000);
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(1));
    const [[id, sent]] = [...t.backend.notes.entries()] as [
      string,
      { tags: string[]; comment: string },
    ][];
    expect(sent).toMatchObject({ tags: ["question"], comment: "From Anna" });
    // Its answer is what the recorder shows next.
    expect(t.status().questionNoteId).toBe(id);
    t.controller.dismissQuestion();
    expect(t.status().questionNoteId).toBeNull();
    // An ordinary note still gets 15 seconds.
    await t.controller.toggle();
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(2));
    const plain = [...t.backend.notes.values()][1] as {
      audioStartMs: number;
    };
    expect(plain.audioStartMs).toBe(75_000);
  });

  it("takes notes from the microphone, without a picture", async () => {
    const t = await setup("until_stopped", true, false, true);
    expect(t.status()).toMatchObject({
      audioOnly: true,
      audioAvailable: true,
      videoAvailable: false,
    });
    t.buffer?.append(ramp(0, 7000)); // 70 seconds said in the room
    t.setClock(70_000);
    // A question: the last minute, and no frame is asked for.
    const asked = await t.controller.question();
    expect(asked).toMatchObject({
      outcome: "saved",
      audio_start_ms: 10_000,
      audio_end_ms: 70_000,
    });
    // An ordinary note opens and closes as usual.
    await t.controller.toggle();
    expect(t.status().openSinceMs).toBe(70_000);
    t.buffer?.append(ramp(7000, 300));
    await t.controller.toggle();
    await vi.waitFor(() => expect(t.backend.notes.size).toBe(2));
    const sent = [...t.backend.notes.values()] as {
      image: Blob | null;
      audio: Blob | null;
      tags: string[];
    }[];
    expect(sent.map((note) => note.image)).toEqual([null, null]);
    expect(sent.every((note) => note.audio !== null)).toBe(true);
    expect(sent.map((note) => note.tags)).toEqual([["question"], []]);
    expect(t.encoded).toEqual([]);
    // The microphone goes away: the reason is the microphone, not a tab.
    t.lose();
    await vi.waitFor(() => expect(t.status().audioAvailable).toBe(false));
    expect(t.status().notice).toBe("mic_lost");
    expect(await t.controller.question()).toEqual({
      outcome: "error",
      reason: "mic_lost",
    });
  });

  it("keeps nothing when the microphone has heard nothing yet", async () => {
    const t = await setup("last_15s", true, false, true);
    expect(await t.controller.toggle()).toEqual({
      outcome: "error",
      reason: "no_audio",
    });
    expect(t.backend.notes.size).toBe(0);
  });
});

describe("slide detector", () => {
  const picture = (value: number, patch = 0) => {
    const cells = new Uint8Array(576).fill(value);
    cells.fill(255 - value, 0, patch);
    return cells;
  };

  it("compares pictures by the share of cells that differ", () => {
    expect(difference(picture(10), picture(10))).toBe(0);
    expect(difference(picture(10), picture(20))).toBe(0); // small flicker
    expect(difference(picture(10), picture(200))).toBe(1);
    expect(difference(picture(10), picture(10, 144))).toBe(0.25);
  });

  it("ignores a slide that builds up bullet by bullet", () => {
    const detector = new SlideDetector();
    detector.feed(picture(10));
    expect(detector.feed(picture(10))).toEqual({ verdict: "same", hold: true });
    // A few new lines each time: the same slide, held again in its new state.
    for (const patch of [20, 20, 40, 40, 60, 60])
      expect(detector.feed(picture(10, patch)).verdict).toBe("same");
    // A different picture: first seen leaving, then still.
    expect(detector.feed(picture(200))).toEqual({
      verdict: "leaving",
      hold: false,
    });
    expect(detector.feed(picture(200))).toEqual({
      verdict: "changed",
      hold: true,
    });
  });
});
