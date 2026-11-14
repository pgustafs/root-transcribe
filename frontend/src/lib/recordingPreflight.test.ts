import { describe, expect, it } from "vitest";

import {
  MIN_RECORDING_BYTES,
  preflightRecording,
  resolveContainer,
} from "./recordingPreflight";

function withMagic(magic: number[], size = 512, offset = 0): Blob {
  const bytes = new Uint8Array(size);
  bytes.set(magic, offset);
  return new Blob([bytes]);
}

describe("preflightRecording", () => {
  it("accepts a well-formed webm", async () => {
    const webm = withMagic([0x1a, 0x45, 0xdf, 0xa3]);
    expect(await preflightRecording(webm, "audio/webm;codecs=opus")).toEqual({
      ok: true,
    });
  });

  it("rejects an empty blob", async () => {
    expect(await preflightRecording(new Blob([]), "audio/webm")).toEqual({
      ok: false,
      reason: "empty",
    });
  });

  it("rejects a too-small blob", async () => {
    const tiny = new Blob([new Uint8Array(MIN_RECORDING_BYTES - 1)]);
    expect(await preflightRecording(tiny, "audio/webm")).toEqual({
      ok: false,
      reason: "too_small",
    });
  });

  it("rejects a webm whose header isn't EBML (the incident shape)", async () => {
    // Cluster id at byte 0, EBML magic missing — a recorder that flushed
    // clusters before the header (issue #37).
    const bad = withMagic([0x1f, 0x43, 0xb6, 0x75]);
    expect(await preflightRecording(bad, "audio/webm")).toEqual({
      ok: false,
      reason: "bad_header",
    });
  });

  it("checks ogg and mp4 magics too", async () => {
    const ogg = withMagic([0x4f, 0x67, 0x67, 0x53]); // "OggS"
    expect(await preflightRecording(ogg, "audio/ogg")).toEqual({ ok: true });
    const mp4 = withMagic([0x66, 0x74, 0x79, 0x70], 512, 4); // "ftyp" at 4
    expect(await preflightRecording(mp4, "audio/mp4")).toEqual({ ok: true });
  });

  it("accepts an unknown container on size alone (server is authoritative)", async () => {
    const unknown = new Blob([new Uint8Array(512)]);
    expect(await preflightRecording(unknown, "audio/x-weird")).toEqual({
      ok: true,
    });
  });

  it("falls back to the filename when the MIME is missing or generic", async () => {
    // File pickers frequently report "" or application/octet-stream — without
    // the extension fallback a malformed .webm would skip header validation.
    const bad = withMagic([0x1f, 0x43, 0xb6, 0x75]);
    expect(await preflightRecording(bad, "", "broken.webm")).toEqual({
      ok: false,
      reason: "bad_header",
    });
    expect(
      await preflightRecording(bad, "application/octet-stream", "broken.webm"),
    ).toEqual({ ok: false, reason: "bad_header" });
    // ...and a good one still passes on the same fallback.
    const good = withMagic([0x1a, 0x45, 0xdf, 0xa3]);
    expect(await preflightRecording(good, "", "fine.webm")).toEqual({
      ok: true,
    });
  });

  it("uses the declared MIME in preference to the extension", async () => {
    const ogg = withMagic([0x4f, 0x67, 0x67, 0x53]);
    // Correct Ogg bytes, misleading .webm name: the MIME wins, so it passes.
    expect(await preflightRecording(ogg, "audio/ogg", "misnamed.webm")).toEqual(
      {
        ok: true,
      },
    );
  });

  it("its minimum matches the authoritative server guard", () => {
    // media_validation.MIN_MEDIA_BYTES on the backend — a lower client bar would
    // wave through objects the worker then rejects asynchronously (issue #37).
    expect(MIN_RECORDING_BYTES).toBe(256);
  });
});

describe("resolveContainer", () => {
  it("maps MIME and extension variants, and gives up cleanly", () => {
    expect(resolveContainer("audio/webm;codecs=opus")).toBe("webm");
    expect(resolveContainer("video/x-matroska")).toBe("webm");
    expect(resolveContainer("audio/ogg")).toBe("ogg");
    expect(resolveContainer("audio/mp4")).toBe("mp4");
    expect(resolveContainer("", "clip.OGG")).toBe("ogg");
    expect(resolveContainer(undefined, "clip.m4a")).toBe("mp4");
    expect(resolveContainer("", "notes.wav")).toBeNull();
    expect(resolveContainer(undefined, undefined)).toBeNull();
  });
});
