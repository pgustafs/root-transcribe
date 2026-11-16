import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { createUploadSession, retryRecording } from "@/api/meetings";
import { server } from "@/test/msw/server";

describe("number of speakers", () => {
  it("goes with a resumable upload", async () => {
    let body: Record<string, unknown> | undefined;
    server.use(
      http.post(
        "*/api/v1/workspaces/:wid/meetings/uploads",
        async ({ request }) => {
          body = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({
            id: "s",
            recording_id: "r",
            part_size_bytes: 1,
          });
        },
      ),
    );
    await createUploadSession("ws-1", {
      title: "Two of us",
      filename: "m.webm",
      participant_consent: true,
      expected_speakers: 2,
    });
    expect(body?.expected_speakers).toBe(2);
  });

  it.each([
    ["changed", { expectedSpeakers: 2 }, { expected_speakers: 2 }],
    [
      "back to automatic",
      { expectedSpeakers: null },
      { expected_speakers: null },
    ],
    ["kept when not given", undefined, null],
  ] as const)("is %s on retry", async (_name, options, expected) => {
    let body: unknown = "not sent";
    server.use(
      http.post(
        "*/api/v1/workspaces/:wid/recordings/:rid/retry",
        async ({ request }) => {
          const text = await request.text();
          body = text ? JSON.parse(text) : null;
          return HttpResponse.json({ id: "r" });
        },
      ),
    );
    await retryRecording("ws-1", "r", options);
    expect(body).toEqual(expected);
  });
});
