import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { I18nextProvider } from "react-i18next";
import { beforeEach, expect, it, vi } from "vitest";

import type { TranscriptCleanup } from "@/api/cleanup";
import type { MeetingSegment } from "@/api/types";
import i18n from "@/i18n";
import { server } from "@/test/msw/server";

import { MeetingCleanedPanel } from "./MeetingCleanedPanel";

const CLEANUP = "*/api/v1/workspaces/w1/recordings/r1/cleanup";

const segment = (
  id: string,
  idx: number,
  speaker: string | null,
  text: string,
): MeetingSegment => ({
  id,
  revision_id: "v1",
  version: 1,
  idx,
  start_ms: 61000 * (idx + 1),
  end_ms: 61000 * (idx + 1) + 4000,
  speaker_label: `SPEAKER_0${idx % 2}`,
  speaker_profile_id: null,
  speaker_name: speaker,
  speaker_confidence: null,
  text,
  is_edited: false,
});

const SEGMENTS = [
  segment(
    "s1",
    0,
    "Thomas",
    "Ska vi ha kvar Bok Demo och läsa om plattformen?",
  ),
  segment("s2", 1, null, "Det kan vi ju ha."),
  segment("s3", 2, "Thomas", "Den raden har någon rättat för hand sedan dess."),
];

let cleanup: TranscriptCleanup | null;
let created: number;

const ready = (): TranscriptCleanup => ({
  id: "c1",
  recording_id: "r1",
  revision_id: "v1",
  status: "ready",
  current_step: null,
  changes: [
    {
      segment_id: "s1",
      original: "Ska vi ha kvar Bok Demo och läsa om plattformen?",
      text: "Ska vi ha kvar Boka demo och läsa om plattformen?",
      status: "suggested",
    },
    {
      // The line was edited after the model read it: not laid over it.
      segment_id: "s3",
      original: "Den raden har någon rätat för hand.",
      text: "Den raden har någon rättat för hand.",
      status: "suggested",
    },
  ],
  rejected: 2,
  error: null,
  model: "qwen",
  duration_ms: 70000,
  created_at: "2026-11-17T10:00:00Z",
  updated_at: "2026-11-17T10:01:10Z",
});

beforeEach(() => {
  cleanup = null;
  created = 0;
  server.use(
    http.get(CLEANUP, () => HttpResponse.json(cleanup)),
    http.post(CLEANUP, () => {
      created += 1;
      // The worker finishes at once in this test.
      cleanup = ready();
      return HttpResponse.json(cleanup, { status: 202 });
    }),
    http.post(`${CLEANUP}/c1/changes/:sid`, async ({ request, params }) => {
      const { decision } = (await request.json()) as { decision: string };
      cleanup = {
        ...cleanup!,
        changes: cleanup!.changes.map((change) =>
          change.segment_id === params.sid
            ? {
                ...change,
                status: decision === "dismiss" ? "dismissed" : "suggested",
              }
            : change,
        ),
      };
      return HttpResponse.json(cleanup);
    }),
    http.delete(`${CLEANUP}/c1`, () => {
      cleanup = null;
      return new HttpResponse(null, { status: 204 });
    }),
  );
});

function showPanel(props: { canCreate?: boolean; isAvailable?: boolean } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onSeek = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <MeetingCleanedPanel
          workspaceId="w1"
          recordingId="r1"
          segments={SEGMENTS}
          canCreate={props.canCreate ?? true}
          isAvailable={props.isAvailable ?? true}
          onSeek={onSeek}
        />
      </I18nextProvider>
    </QueryClientProvider>,
  );
  return { onSeek };
}

it("makes a cleaned version and shows each correction on its line", async () => {
  const user = userEvent.setup();
  const { onSeek } = showPanel();
  await user.click(
    await screen.findByRole("button", { name: "Create cleaned version" }),
  );
  expect(created).toBe(1);

  // One correction applies; the other line was edited since and stands as is.
  expect(await screen.findByText(/1 line corrected/)).toBeInTheDocument();
  expect(screen.getByText(/2 suggestions left out/)).toBeInTheDocument();
  const rows = screen.getAllByRole("article");
  expect(rows).toHaveLength(3);
  // What was replaced and what replaced it, both readable.
  expect(within(rows[0]).getByText("Bok Demo").closest("del")).not.toBeNull();
  expect(within(rows[0]).getByText("Boka demo").closest("ins")).not.toBeNull();
  expect(within(rows[0]).getByText("Corrected")).toBeInTheDocument();
  expect(within(rows[0]).getByText("Thomas")).toBeInTheDocument();
  // Untouched lines read as in the transcript, with a friendly speaker name.
  expect(within(rows[1]).getByText("Det kan vi ju ha.")).toBeInTheDocument();
  expect(within(rows[1]).getByText("Speaker 2")).toBeInTheDocument();
  expect(
    within(rows[2]).getByText(
      "Den raden har någon rättat för hand sedan dess.",
    ),
  ).toBeInTheDocument();
  expect(within(rows[2]).queryByText("Corrected")).toBeNull();

  // The time jumps the player there.
  await user.click(within(rows[1]).getByRole("button", { name: /02:02/ }));
  expect(onSeek).toHaveBeenCalledWith(122000);

  // Keep the original line, then take the correction back.
  await user.click(
    within(rows[0]).getByRole("button", { name: "Keep original" }),
  );
  await waitFor(() =>
    expect(
      within(screen.getAllByRole("article")[0]).getByText("Original kept"),
    ).toBeInTheDocument(),
  );
  const kept = screen.getAllByRole("article")[0];
  expect(
    within(kept).getByText("Ska vi ha kvar Bok Demo och läsa om plattformen?"),
  ).toBeInTheDocument();
  expect(within(kept).queryByText("Boka demo")).toBeNull();
  await user.click(
    within(kept).getByRole("button", { name: "Use correction" }),
  );
  await waitFor(() =>
    expect(
      within(screen.getAllByRole("article")[0]).getByText("Boka demo"),
    ).toBeInTheDocument(),
  );

  // Only the corrected lines, on request.
  await user.click(screen.getByRole("checkbox"));
  expect(screen.getAllByRole("article")).toHaveLength(1);

  // Removing it leaves the tab as it started.
  await user.click(screen.getByRole("button", { name: "Remove" }));
  expect(
    await screen.findByRole("button", { name: "Create cleaned version" }),
  ).toBeInTheDocument();
  expect(screen.queryAllByRole("article")).toHaveLength(0);
});

it("shows how far the model has read", async () => {
  cleanup = {
    ...ready(),
    status: "processing",
    current_step: "part:2/7",
    changes: [],
  };
  showPanel();
  expect(await screen.findByText("Part 2 of 7")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Reading the transcript…" }),
  ).toBeDisabled();
});

it("explains a failure and offers to try again", async () => {
  cleanup = {
    ...ready(),
    status: "failed",
    error: "The model timed out.",
    changes: [],
  };
  showPanel();
  expect(await screen.findByText("The model timed out.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Clean again" })).toBeEnabled();
});

it("is read-only without the right, and says when no model is configured", async () => {
  cleanup = ready();
  showPanel({ canCreate: false, isAvailable: false });
  expect(await screen.findByText("Boka demo")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Keep original" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Clean again" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
  expect(
    screen.getByText("No language model is configured"),
  ).toBeInTheDocument();
});
