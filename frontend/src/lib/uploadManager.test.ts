/**
 * uploadManager + recordingStore (issue #6, wave B4): resumable multipart
 * upload against MSW, and IndexedDB persistence via fake-indexeddb.
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  appendChunk,
  assembleRecording,
  createStoredSession,
  deleteStoredSession,
  getStoredSession,
  listStoredSessions,
  purgeExpiredSessions,
  updateStoredSession,
} from "@/lib/recordingStore";
import { createRecording } from "@/api/meetings";
import { sha256Hex, uploadRecording } from "@/lib/uploadManager";
import { meetingsStore, resetStore } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { HttpResponse, http } from "msw";

beforeEach(() => {
  resetStore();
});

describe("recordingStore", () => {
  it("persists chunks and reassembles the recording in order", async () => {
    const id = await createStoredSession({
      workspaceId: "ws-1",
      title: "Standup",
      consent: true,
      mimeType: "audio/webm",
    });
    // Out-of-order writes must still assemble by sequence.
    await appendChunk(id, 1, new Blob(["-two"]));
    await appendChunk(id, 0, new Blob(["one"]));
    await appendChunk(id, 2, new Blob(["-three"]));

    const { blob } = await assembleRecording(id, "audio/webm");
    expect(await blob.text()).toBe("one-two-three");

    const sessions = await listStoredSessions();
    expect(sessions.map((s) => s.id)).toContain(id);

    await deleteStoredSession(id);
    expect(await getStoredSession(id)).toBeUndefined();
    expect((await assembleRecording(id, "audio/webm")).blob.size).toBe(0);
  });

  it("purges sessions past the local retention window", async () => {
    const id = await createStoredSession({
      workspaceId: "ws-1",
      title: "Old",
      consent: false,
      mimeType: "audio/webm",
    });
    await updateStoredSession(id, { state: "pending-upload" });
    // Nothing is old enough yet.
    expect(await purgeExpiredSessions()).toBe(0);
    // With a zero-length window everything expires.
    expect(await purgeExpiredSessions(0)).toBeGreaterThanOrEqual(1);
    expect(await getStoredSession(id)).toBeUndefined();
  });
});

describe("uploadRecording", () => {
  const blob = () => new Blob(["0123456789abcdefXYZ"]); // 19 bytes → 3 parts @ 8

  it("uploads parts with checksums and completes with a manifest", async () => {
    const progress: number[] = [];
    const parts: number[] = [];
    let session: { uploadSessionId: string } | null = null;
    const rec = await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
      onSession: (info) => {
        session = info;
      },
      onPartDone: (n) => {
        parts.push(n);
      },
      onProgress: (p) => progress.push(p.partsDone),
    });
    expect(rec.id).toBe("rec-upload");
    expect(parts).toEqual([1, 2, 3]);
    expect(progress.at(-1)).toBe(3);
    const stored = meetingsStore.uploadSessions[session!.uploadSessionId];
    expect(stored.completed).toBe(true);
    // Every stored part checksum matches what we would compute locally.
    const expected = await sha256Hex(await blob().slice(0, 8).arrayBuffer());
    expect(stored.parts[1]).toBe(expected);
  });

  it("retries a checksum-422 part once and still completes", async () => {
    meetingsStore.failNextParts = 1;
    const rec = await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
    });
    expect(rec.id).toBe("rec-upload");
    expect(meetingsStore.failNextParts).toBe(0);
  });

  it("resume skips parts the server already accepted", async () => {
    // First pass: create the session and upload only part 1, then "crash".
    let info: {
      uploadSessionId: string;
      recordingId: string;
      partSizeBytes: number;
    } | null = null;
    const uploaded: Record<number, string> = {};
    const controller = new AbortController();
    await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
      signal: controller.signal,
      onSession: (i) => {
        info = i;
      },
      onPartDone: (n, sha) => {
        uploaded[n] = sha;
        if (n === 1) controller.abort(); // interrupt after the first part
      },
    }).catch(() => undefined);
    expect(info).not.toBeNull();
    // The abort discarded the server session; recreate the scenario with a
    // fresh one carrying part 1 (as a reload-resume would find it).
    const sid = info!.uploadSessionId;
    meetingsStore.uploadSessions[sid].aborted = false;

    const partPuts: number[] = [];
    const rec = await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
      resume: { ...info!, uploadedParts: uploaded },
      onPartDone: (n) => {
        partPuts.push(n);
      },
    });
    expect(rec.id).toBe("rec-upload");
    expect(partPuts).toEqual([2, 3]); // part 1 was skipped
    expect(meetingsStore.uploadSessions[sid].completed).toBe(true);
  });

  it("abort truly cancels: server session deleted, AbortError thrown", async () => {
    const controller = new AbortController();
    controller.abort();
    let sid: string | null = null;
    await expect(
      uploadRecording({
        workspaceId: "ws-1",
        blob: blob(),
        filename: "meeting.webm",
        title: "Standup",
        consent: true,
        signal: controller.signal,
        onSession: (i) => {
          sid = i.uploadSessionId;
        },
      }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(meetingsStore.uploadSessions[sid!].aborted).toBe(true);
  });

  it("cancel during the 425 wait issues no further completion request", async () => {
    // The server says "another caller is completing this right now". Previously
    // the signal was only consulted AFTER a failure, and the wait itself was a
    // bare setTimeout — so a cancel arriving during the delay was ignored for a
    // full 15s interval and the loop then sent ANOTHER completion for an upload
    // the user had cancelled (issue #37 review round 12). Without the fix this
    // test does not merely fail its assertion, it hangs out the retry interval.
    const controller = new AbortController();
    let completions = 0;
    server.use(
      http.post(
        "*/api/v1/workspaces/:wid/meetings/uploads/:sid/complete",
        () => {
          completions += 1;
          // The user hits Cancel while we are waiting out the 425.
          setTimeout(() => controller.abort(), 0);
          return HttpResponse.json(
            { detail: "This upload is still being completed" },
            { status: 425 },
          );
        },
      ),
    );

    let sid: string | null = null;
    const started = performance.now();
    await expect(
      uploadRecording({
        workspaceId: "ws-1",
        blob: blob(),
        filename: "meeting.webm",
        title: "Standup",
        consent: true,
        signal: controller.signal,
        onSession: (i) => {
          sid = i.uploadSessionId;
        },
      }),
    ).rejects.toMatchObject({ name: "AbortError" });

    // Exactly ONE completion request: the cancel stopped the retry loop before
    // it could issue a second.
    expect(completions).toBe(1);
    // And it stopped PROMPTLY. With a bare setTimeout the cancel sat unnoticed
    // for the whole 15s retry interval — the UI kept saying "uploading" long
    // after the user cancelled. Threshold is a third of that interval.
    expect(performance.now() - started).toBeLessThan(5_000);
    // Nothing was uploaded, and the session was cleaned up server-side.
    expect(meetingsStore.uploadSessions[sid!].completed).toBe(false);
    expect(meetingsStore.uploadSessions[sid!].aborted).toBe(true);
    expect(meetingsStore.recordings).toHaveLength(0);
  });

  it("a cancel the server refuses still stops the client", async () => {
    // Cancel is refused while a completion holds a fresh lease. The client must
    // still stop — it may not keep uploading an upload the user cancelled just
    // because the cleanup call came back 409 (issue #37 review round 12).
    const controller = new AbortController();
    let deletes = 0;
    server.use(
      http.delete("*/api/v1/workspaces/:wid/meetings/uploads/:sid", () => {
        deletes += 1;
        return HttpResponse.json(
          { detail: "This upload is being completed" },
          { status: 409 },
        );
      }),
    );

    let sid: string | null = null;
    await expect(
      uploadRecording({
        workspaceId: "ws-1",
        blob: blob(),
        filename: "meeting.webm",
        title: "Standup",
        consent: true,
        signal: controller.signal,
        onSession: (i) => {
          sid = i.uploadSessionId;
        },
        onPartDone: () => {
          controller.abort(); // cancel mid-upload
        },
      }),
    ).rejects.toMatchObject({ name: "AbortError", serverConfirmed: false });
    expect(deletes).toBe(1); // cleanup attempted, and its REFUSAL reported
    expect(meetingsStore.uploadSessions[sid!].completed).toBe(false);
  });

  it("cancel aborts a completion POST that is still pending", async () => {
    // The 425 loop is not the only exposure: the completion request itself can
    // be pending for a long time. Without the signal on the request, Cancel was
    // invisible until the POST resolved — and its 202 was then ACCEPTED, so the
    // recording went on to transcription after the user cancelled (issue #37
    // review round 13).
    const controller = new AbortController();
    let late = false;
    server.use(
      http.post(
        "*/api/v1/workspaces/:wid/meetings/uploads/:sid/complete",
        async () => {
          setTimeout(() => controller.abort(), 0);
          // Far longer than this suite's 20s timeout: the ONLY way the upload
          // promise settles quickly is the cancel cutting the request.
          await new Promise((resolve) => setTimeout(resolve, 30_000));
          late = true;
          return HttpResponse.json({ id: "rec-late" }, { status: 202 });
        },
      ),
    );

    let sid: string | null = null;
    const started = performance.now();
    const result = await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
      signal: controller.signal,
      onSession: (i) => {
        sid = i.uploadSessionId;
      },
    }).catch((e) => e);

    expect(result).toMatchObject({ name: "AbortError" });
    // The request was ABANDONED, not awaited: without the signal on the POST
    // this promise stays pending until the handler answers 202 — well past the
    // suite timeout — and that 202 is then accepted despite the cancel.
    expect(performance.now() - started).toBeLessThan(5_000);
    expect(late).toBe(false);
    // No recording was accepted, and the session was cancelled server-side.
    expect(meetingsStore.uploadSessions[sid!].aborted).toBe(true);
    expect(meetingsStore.recordings).toHaveLength(0);
  });

  it("cancel interrupts a part PUT that is still pending", async () => {
    // The signal reached completion but NOT the part uploads, so a cancel
    // during a stalled PUT was invisible: the recorder bar disappeared while
    // the upload carried on, and the server never received the DELETE (issue
    // #37 review round 14).
    const controller = new AbortController();
    let deletes = 0;
    let late = false;
    server.use(
      http.put(
        "*/api/v1/workspaces/:wid/meetings/uploads/:sid/parts/:n",
        async () => {
          setTimeout(() => controller.abort(), 0);
          // Far past the suite timeout: only the cancel can end this request.
          await new Promise((resolve) => setTimeout(resolve, 30_000));
          late = true;
          return HttpResponse.json({ part_number: 1 });
        },
      ),
      http.delete("*/api/v1/workspaces/:wid/meetings/uploads/:sid", () => {
        deletes += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const started = performance.now();
    const error = await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
      signal: controller.signal,
    }).catch((e) => e);

    expect(error).toMatchObject({ name: "AbortError", serverConfirmed: true });
    expect(performance.now() - started).toBeLessThan(5_000);
    expect(late).toBe(false); // the PUT was abandoned, not awaited
    expect(deletes).toBe(1); // and the server WAS told to discard the session
  });

  it("cancel during the CHECKSUM RETRY still cleans up", async () => {
    // The retry is nested inside the first attempt's catch, so it had no abort
    // handler of its own: a cancel while it was pending escaped as an ordinary
    // CanceledError and the DELETE was never issued (issue #37 r15). The
    // first PUT 422s, the retry hangs, and Cancel lands on the retry.
    const controller = new AbortController();
    let attempts = 0;
    let deletes = 0;
    let late = false;
    server.use(
      http.put(
        "*/api/v1/workspaces/:wid/meetings/uploads/:sid/parts/:n",
        async () => {
          attempts += 1;
          if (attempts === 1) {
            // Corruption in flight — the client re-sends this part once.
            return HttpResponse.json({ detail: "checksum" }, { status: 422 });
          }
          setTimeout(() => controller.abort(), 0);
          await new Promise((resolve) => setTimeout(resolve, 30_000));
          late = true;
          return HttpResponse.json({ part_number: 1 });
        },
      ),
      http.delete("*/api/v1/workspaces/:wid/meetings/uploads/:sid", () => {
        deletes += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const started = performance.now();
    const error = await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
      signal: controller.signal,
    }).catch((e) => e);

    expect(attempts).toBe(2); // the 422 really did trigger the retry
    expect(error).toMatchObject({ name: "AbortError", serverConfirmed: true });
    expect(performance.now() - started).toBeLessThan(5_000);
    expect(late).toBe(false);
    expect(deletes).toBe(1); // cleanup ran, from INSIDE the retry
  });

  it("a 404 from the cleanup counts as CONFIRMED cancellation", async () => {
    // The lost-204 case: the server committed the cancellation but the response
    // never arrived. The retry finds nothing — which is not a failure, it is
    // the outcome. Reporting it as unconfirmed kept the client holding ids the
    // server had already destroyed (issue #37 review round 14).
    const controller = new AbortController();
    server.use(
      http.delete(
        "*/api/v1/workspaces/:wid/meetings/uploads/:sid",
        () => new HttpResponse(null, { status: 404 }),
      ),
    );

    const error = await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
      signal: controller.signal,
      onPartDone: () => {
        controller.abort();
      },
    }).catch((e) => e);

    expect(error).toMatchObject({ name: "AbortError", serverConfirmed: true });
  });

  it("reports serverConfirmed=false when the real 409 owner refuses", async () => {
    // The production refusal, from the MSW handler rather than a per-test
    // override: while a completion lease is HELD the server answers 425 to
    // completion and 409 to cancel. The flag is independent of the 425
    // countdown on purpose — deriving it from that counter meant the handler
    // decremented before the DELETE arrived, so the cancel always succeeded and
    // this path was never exercised (issue #37 review round 13).
    meetingsStore.completionsInFlight = 5;
    meetingsStore.completionLeaseHeld = true;
    const controller = new AbortController();
    server.use(
      http.post(
        "*/api/v1/workspaces/:wid/meetings/uploads/:sid/complete",
        () => {
          setTimeout(() => controller.abort(), 0);
          return HttpResponse.json(
            { detail: "This upload is still being completed" },
            { status: 425 },
          );
        },
      ),
    );

    let sid: string | null = null;
    const error = await uploadRecording({
      workspaceId: "ws-1",
      blob: blob(),
      filename: "meeting.webm",
      title: "Standup",
      consent: true,
      signal: controller.signal,
      onSession: (i) => {
        sid = i.uploadSessionId;
      },
    }).catch((e) => e);

    expect(error).toMatchObject({ name: "AbortError", serverConfirmed: false });
    // The server kept the session: it is NOT aborted, so the upload it is
    // completing may still land — which is exactly why the caller must keep
    // the local copy.
    expect(meetingsStore.uploadSessions[sid!].aborted).toBe(false);
  });
});

it.each(["auto", "en", "sv"] as const)(
  "sends %s language when opening an upload",
  async (requestedLanguage) => {
    let sent: unknown;
    server.use(
      http.post(
        "/api/v1/workspaces/:workspaceId/meetings/uploads",
        async ({ request }) => {
          sent = await request.json();
          return HttpResponse.json({
            id: "language-session",
            recording_id: "rec-upload",
            part_size_bytes: 8,
          });
        },
      ),
    );
    const controller = new AbortController();
    // Stop after session creation: the assertion concerns the upload contract.
    await expect(
      uploadRecording({
        workspaceId: "ws-1",
        blob: new Blob(["audio"]),
        filename: "a.webm",
        title: "Language",
        consent: true,
        requestedLanguage,
        signal: controller.signal,
        onSession: () => controller.abort(),
      }),
    ).rejects.toThrow();
    expect(sent).toMatchObject({ requested_language: requestedLanguage });
  },
);

it.each(["auto", "en", "sv"] as const)(
  "sends %s language with a file upload",
  async (requestedLanguage) => {
    let language: unknown;
    server.use(
      http.post("*/api/v1/workspaces/:wid/recordings", async ({ request }) => {
        language = (await request.formData()).get("requested_language");
        return HttpResponse.json({ id: "uploaded" });
      }),
    );
    await createRecording("ws-1", {
      file: new Blob(["audio"]),
      filename: "a.wav",
      title: "Language",
      consent: true,
      requestedLanguage,
    });
    expect(language).toBe(requestedLanguage);
  },
);
