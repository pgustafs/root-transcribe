import { http, HttpResponse } from "msw";
import type { MeetingRecording } from "@/api/types";
export const meetingsStore: {
  recordings: MeetingRecording[];
  uploadDiagnostics: Record<string, unknown>[];
  clientEvents: Record<string, unknown>[];
  uploadSessions: Record<
    string,
    {
      parts: Record<number, string>;
      completed: boolean;
      aborted: boolean;
      partSizeBytes: number;
    }
  >;
  failNextParts: number;
  completionsInFlight: number;
  completionLeaseHeld: boolean;
} = {
  recordings: [],
  uploadDiagnostics: [],
  clientEvents: [],
  uploadSessions: {},
  failNextParts: 0,
  completionsInFlight: 0,
  completionLeaseHeld: false,
};
export function resetStore() {
  meetingsStore.recordings = [];
  meetingsStore.uploadDiagnostics = [];
  meetingsStore.clientEvents = [];
  meetingsStore.uploadSessions = {};
  meetingsStore.failNextParts = 0;
  meetingsStore.completionsInFlight = 0;
  meetingsStore.completionLeaseHeld = false;
}
export const handlers = [
  http.post("*/api/v1/workspaces/:wid/meetings/uploads", () => {
    const id = `up-${Object.keys(meetingsStore.uploadSessions).length + 1}`;
    // Tiny part size so tests exercise multi-part flows with small blobs.
    meetingsStore.uploadSessions[id] = {
      parts: {},
      completed: false,
      aborted: false,
      partSizeBytes: 8,
    };
    return HttpResponse.json(
      {
        id,
        recording_id: meetingsStore.recordings[0]?.id ?? "rec-upload",
        part_size_bytes: 8,
        status: "active",
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
        created_at: new Date().toISOString(),
      },
      { status: 201 },
    );
  }),
  http.put(
    "*/api/v1/workspaces/:wid/meetings/uploads/:sid/parts/:n",
    async ({ params, request }) => {
      const session = meetingsStore.uploadSessions[params.sid as string];
      if (!session || session.aborted)
        return HttpResponse.json(null, { status: 404 });
      if (meetingsStore.failNextParts > 0) {
        meetingsStore.failNextParts -= 1;
        return HttpResponse.json(
          { detail: "Part checksum mismatch; re-send the part" },
          { status: 422 },
        );
      }
      const sha = request.headers.get("X-Part-Sha256") ?? "";
      session.parts[Number(params.n)] = sha;
      return HttpResponse.json({ part_number: Number(params.n) });
    },
  ),
  http.post(
    "*/api/v1/workspaces/:wid/meetings/uploads/:sid/complete",
    async ({ params, request }) => {
      const session = meetingsStore.uploadSessions[params.sid as string];
      if (!session) return HttpResponse.json(null, { status: 404 });
      // Production is IDEMPOTENT for a repeat completion (issue #37 r9): it
      // returns the recording it already produced, which is what stops a
      // timed-out client from uploading the same meeting twice. An ABORTED
      // session is genuinely dead and still conflicts. Tests that need a
      // conflict override this handler explicitly.
      if (session.aborted)
        return HttpResponse.json(
          { detail: "This upload session is no longer active" },
          { status: 409 },
        );
      if (session.completed)
        return HttpResponse.json(meetingsStore.recordings[0] ?? null, {
          status: 202,
        });
      if (meetingsStore.completionsInFlight > 0) {
        meetingsStore.completionsInFlight -= 1;
        // 425 Too Early: retryable, NOT a success and NOT "dead".
        return HttpResponse.json(
          { detail: "This upload is still being completed" },
          { status: 425 },
        );
      }
      const body = (await request.json()) as {
        parts: { part_number: number; sha256: string }[];
        client_diagnostics?: Record<string, unknown>;
      };
      if (body.client_diagnostics)
        meetingsStore.uploadDiagnostics.push(body.client_diagnostics);
      const stored = Object.keys(session.parts).map(Number).sort();
      const claimed = body.parts.map((p) => p.part_number).sort();
      if (JSON.stringify(stored) !== JSON.stringify(claimed))
        return HttpResponse.json(
          { detail: "manifest mismatch" },
          { status: 409 },
        );
      session.completed = true;
      const rec = meetingsStore.recordings[0] ?? null;
      return HttpResponse.json(
        rec ?? {
          id: "rec-upload",
          workspace_id: params.wid,
          title: "Uploaded",
          status: "pending",
        },
        { status: 202 },
      );
    },
  ),
  http.post(
    "*/api/v1/workspaces/:wid/meetings/client-events",
    async ({ request }) => {
      meetingsStore.clientEvents.push(
        (await request.json()) as (typeof meetingsStore.clientEvents)[number],
      );
      return new HttpResponse(null, { status: 202 });
    },
  ),
  http.delete(
    "*/api/v1/workspaces/:wid/meetings/uploads/:sid",
    ({ params }) => {
      const session = meetingsStore.uploadSessions[params.sid as string];
      // Production refuses cancel while a completion holds a FRESH lease —
      // aborting the multipart upload underneath it would destroy a live
      // recording (issue #37 r12). Mirror that, or a UI test would "prove"
      // a cleanup the server would have rejected.
      if (meetingsStore.completionLeaseHeld)
        return HttpResponse.json(
          {
            detail:
              "This upload is being completed and cannot be cancelled right now",
          },
          { status: 409 },
        );
      if (session) session.aborted = true;
      return new HttpResponse(null, { status: 204 });
    },
  ),
];
