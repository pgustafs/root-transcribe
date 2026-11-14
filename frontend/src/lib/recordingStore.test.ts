import { describe, expect, it } from "vitest";

import {
  appendChunk,
  assembleRecording,
  assemblyDiagnostics,
  createStoredSession,
  getStoredSession,
  listChunkMeta,
  noteExpectedChunk,
  updateStoredSession,
} from "./recordingStore";

async function newSession(): Promise<string> {
  return createStoredSession({
    workspaceId: "ws",
    title: "t",
    consent: true,
    mimeType: "audio/webm",
  });
}

describe("assembleRecording integrity (issue #37)", () => {
  it("assembles chunks strictly by sequence with no gaps", async () => {
    const id = await newSession();
    // Persist out of order — assembly must still order by seq.
    await appendChunk(id, 1, new Blob(["b"]));
    await appendChunk(id, 0, new Blob(["a"]));
    await appendChunk(id, 2, new Blob(["c"]));
    const r = await assembleRecording(id, "audio/webm");
    expect(r.chunkCount).toBe(3);
    expect(r.missing).toEqual([]);
    expect(await r.blob.text()).toBe("abc");
  });

  it("reports missing sequences (a lost fire-and-forget write)", async () => {
    const id = await newSession();
    await appendChunk(id, 0, new Blob(["a"]));
    await appendChunk(id, 2, new Blob(["c"])); // seq 1 never landed
    const r = await assembleRecording(id, "audio/webm");
    expect(r.chunkCount).toBe(2);
    expect(r.missing).toEqual([1]);
  });

  it("DETECTS a re-written sequence instead of silently overwriting it", async () => {
    const id = await newSession();
    await appendChunk(id, 0, new Blob(["a"]));
    await appendChunk(id, 0, new Blob(["A"]));
    const r = await assembleRecording(id, "audio/webm");
    expect(r.chunkCount).toBe(1);
    // put() overwrote the bytes, but the anomaly is recorded so callers refuse
    // the recording rather than absorbing it (issue #37 review).
    expect(r.duplicates).toEqual([0]);
  });

  it("an empty session has no chunks and no missing gaps", async () => {
    const id = await newSession();
    const r = await assembleRecording(id, "audio/webm");
    expect(r.chunkCount).toBe(0);
    expect(r.missing).toEqual([]);
    expect(r.blob.size).toBe(0);
  });

  it("detects a lost FINAL chunk via the recorded intent", async () => {
    // Intent-first: seq 3 was announced but its data write never landed. Deriving
    // the range from the highest STORED seq would report no gap at all.
    const id = await newSession();
    for (const seq of [0, 1, 2]) {
      await noteExpectedChunk(id, seq);
      await appendChunk(id, seq, new Blob([`c${seq}`]));
    }
    await noteExpectedChunk(id, 3); // data write lost
    const r = await assembleRecording(id, "audio/webm");
    expect(r.chunkCount).toBe(3);
    expect(r.missing).toEqual([3]);
  });

  it("detects a session whose chunk writes were ALL lost", async () => {
    const id = await newSession();
    await noteExpectedChunk(id, 0);
    await noteExpectedChunk(id, 1);
    const r = await assembleRecording(id, "audio/webm");
    expect(r.blob.size).toBe(0);
    // Not "nothing was recorded" — two chunks are known to be missing, so the
    // caller reports it instead of silently discarding the session.
    expect(r.missing).toEqual([0, 1]);
  });

  it("an authoritative expectedChunks written at stop also reveals a gap", async () => {
    const id = await newSession();
    await appendChunk(id, 0, new Blob(["a"]));
    // The recorder emitted 3 slices; only the first survived.
    await updateStoredSession(id, { expectedChunks: 3 });
    const r = await assembleRecording(id, "audio/webm");
    expect(r.missing).toEqual([1, 2]);
  });

  it("noteExpectedChunk only ever raises the high-water mark", async () => {
    const id = await newSession();
    await noteExpectedChunk(id, 5);
    await noteExpectedChunk(id, 2);
    expect((await getStoredSession(id))?.expectedChunks).toBe(6);
  });

  it("separates recorder timing from client persistence delay", async () => {
    // Wall-clock gaps include OUR callback/persistence latency; the recorder's
    // own timecodes do not. Reporting both (and their skew) is what tells
    // "the browser stalled" from "we were slow" (issue #37 review round 6).
    const id = await newSession();
    await appendChunk(id, 0, new Blob(["a"]), {
      recordedAt: 1_000,
      timecode: 0,
    });
    await appendChunk(id, 1, new Blob(["bb"]), {
      recordedAt: 2_000, // 1000 ms wall
      timecode: 1_000, // 1000 ms media -> no skew
    });
    await appendChunk(id, 2, new Blob(["ccc"]), {
      recordedAt: 6_000, // 4000 ms wall
      timecode: 2_000, // 1000 ms media -> 3000 ms skew: WE lagged
    });

    const diag = await assemblyDiagnostics(id);
    expect(diag.chunk_count).toBe(3);
    expect(diag.total_bytes).toBe(6);
    expect(diag.max_gap_ms).toBe(4_000);
    expect(diag.max_media_gap_ms).toBe(1_000);
    expect(diag.max_skew_ms).toBe(3_000);
  });

  it("reports a stalled RECORDER as a media gap, not skew", async () => {
    const id = await newSession();
    await appendChunk(id, 0, new Blob(["a"]), { recordedAt: 0, timecode: 0 });
    // The recorder itself skipped 5 s; our handling kept pace with it.
    await appendChunk(id, 1, new Blob(["b"]), {
      recordedAt: 5_000,
      timecode: 5_000,
    });
    const diag = await assemblyDiagnostics(id);
    expect(diag.max_gap_ms).toBe(5_000);
    expect(diag.max_media_gap_ms).toBe(5_000);
    expect(diag.max_skew_ms).toBe(0);
  });

  it("omits media timing when the browser provides no timecodes", async () => {
    const id = await newSession();
    await appendChunk(id, 0, new Blob(["a"]), { recordedAt: 0 });
    await appendChunk(id, 1, new Blob(["b"]), { recordedAt: 2_000 });
    const diag = await assemblyDiagnostics(id);
    expect(diag.max_gap_ms).toBe(2_000);
    expect(diag.max_media_gap_ms).toBeNull();
    expect(diag.max_skew_ms).toBeNull();
  });

  it("stores per-chunk timing metadata for diagnostics", async () => {
    const id = await newSession();
    await appendChunk(id, 0, new Blob(["abc"]), {
      recordedAt: 1234,
      timecode: 1000,
    });
    await appendChunk(id, 1, new Blob(["defgh"]));

    // Assert the VALUES round-trip, not merely that a write happened.
    const rows = await listChunkMeta(id);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({
      seq: 0,
      recordedAt: 1234,
      timecode: 1000,
      size: 3,
    });
    // Defaults when the browser gives no timecode: a stamp, and never NaN.
    expect(rows[1].timecode).toBeNull();
    expect(rows[1].size).toBe(5);
    expect(typeof rows[1].recordedAt).toBe("number");
    expect(rows[1].recordedAt).toBeGreaterThan(0);
  });
});

it.each(["auto", "en", "sv"] as const)(
  "preserves %s language for recording recovery",
  async (requestedLanguage) => {
    const id = await createStoredSession({
      workspaceId: "ws",
      title: "Language",
      consent: true,
      mimeType: "audio/webm",
      requestedLanguage,
    });
    await appendChunk(id, 0, new Blob(["audio"]));
    await updateStoredSession(id, { state: "pending-upload" });
    expect((await getStoredSession(id))?.requestedLanguage).toBe(
      requestedLanguage,
    );
  },
);
