import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { I18nextProvider } from "react-i18next";
import { beforeEach, expect, it, vi } from "vitest";

import type { MeetingSegment, SpeakerNameRun } from "@/api/types";
import { ToastProvider } from "@/components/toast/ToastProvider";
import i18n from "@/i18n";
import { server } from "@/test/msw/server";

import { SpeakersDialog } from "./SpeakersDialog";

const BASE = "*/api/v1/workspaces/w1/recordings/r1";

const segment = (
  idx: number,
  label: string,
  name: string | null = null,
): MeetingSegment => ({
  id: `s${idx}`,
  revision_id: "v1",
  version: 1,
  idx,
  start_ms: idx * 5000,
  end_ms: idx * 5000 + 4000,
  speaker_label: label,
  speaker_profile_id: null,
  speaker_name: name,
  speaker_confidence: null,
  text: "…",
  is_edited: false,
});
const SEGMENTS = [
  segment(0, "SPEAKER_00", "Anna"),
  segment(1, "SPEAKER_01"),
  segment(2, "SPEAKER_00", "Anna"),
];

const runWith = (over: Partial<SpeakerNameRun>): SpeakerNameRun => ({
  id: "run1",
  recording_id: "r1",
  revision_id: "v1",
  status: "ready",
  suggestions: [],
  error: null,
  model: "m",
  duration_ms: 900,
  created_at: "2026-11-15T10:00:00Z",
  updated_at: "2026-11-15T10:00:00Z",
  ...over,
});
const peter = {
  speaker_label: "SPEAKER_01",
  name: "Peter",
  quote: "hi my name is Peter",
  start_ms: 5000,
  confidence: "high" as const,
  status: "pending" as const,
};

let run: SpeakerNameRun | null;
let assigned: unknown[];
let decided: unknown[];

beforeEach(() => {
  run = null;
  assigned = [];
  decided = [];
  server.use(
    http.get(`${BASE}/speaker-names`, () => HttpResponse.json(run)),
    http.post(`${BASE}/speaker-names/identify`, () => {
      run = runWith({ suggestions: [peter] });
      return HttpResponse.json(runWith({ status: "pending" }), { status: 202 });
    }),
    http.post(
      `${BASE}/speaker-names/:run/suggestions/:index`,
      async ({ request, params }) => {
        const body = (await request.json()) as { decision: string };
        decided.push({ index: params.index, ...body });
        run = runWith({
          suggestions: [
            {
              ...peter,
              status: body.decision === "accept" ? "accepted" : "dismissed",
            },
          ],
        });
        return HttpResponse.json(run);
      },
    ),
    http.post(`${BASE}/assign-speaker`, async ({ request }) => {
      assigned.push(await request.json());
      return HttpResponse.json({ updated_segments: 1 });
    }),
  );
});

function showDialog(props: Partial<Parameters<typeof SpeakersDialog>[0]> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onClose = vi.fn();
  const onSeek = vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <SpeakersDialog
            workspaceId="w1"
            recordingId="r1"
            segments={SEGMENTS}
            canIdentify={true}
            onSeek={onSeek}
            onClose={onClose}
            {...props}
          />
        </ToastProvider>
      </QueryClientProvider>
    </I18nextProvider>,
  );
  return { onClose, onSeek };
}

it("renames speakers by hand and clears a name", async () => {
  const user = userEvent.setup();
  const { onClose } = showDialog();
  const first = screen.getByLabelText(/Speaker 1/);
  const second = screen.getByLabelText(/Speaker 2/);
  expect(first).toHaveValue("Anna");
  expect(second).toHaveValue("");

  await user.clear(first);
  await user.type(second, " Peter ");
  await user.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(onClose).toHaveBeenCalled());
  expect(assigned).toEqual([
    { speaker_label: "SPEAKER_00", clear_speaker: true },
    { speaker_label: "SPEAKER_01", speaker_name: "Peter" },
  ]);
});

it("saves nothing when no name was changed", async () => {
  const user = userEvent.setup();
  const { onClose } = showDialog();
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(onClose).toHaveBeenCalled();
  expect(assigned).toEqual([]);
});

it("finds a name in the transcript and uses it on request", async () => {
  const user = userEvent.setup();
  const { onSeek } = showDialog();
  await user.click(
    await screen.findByRole("button", { name: "Find names in the transcript" }),
  );
  // The run is polled until it is ready; the suggestion shows its evidence.
  expect(
    await screen.findByText("“hi my name is Peter”", {}, { timeout: 5000 }),
  ).toBeInTheDocument();
  expect(screen.getByText(/1 name found/)).toBeInTheDocument();
  // Nothing was applied by the model.
  expect(assigned).toEqual([]);
  expect(decided).toEqual([]);

  await user.click(screen.getByRole("button", { name: /^Listen at/ }));
  expect(onSeek).toHaveBeenCalledWith(5000);

  await user.click(
    screen.getByRole("button", { name: "Use Peter for Speaker 2" }),
  );
  await waitFor(() =>
    expect(decided).toEqual([{ index: "0", decision: "accept" }]),
  );
  await waitFor(() =>
    expect(screen.queryByText("“hi my name is Peter”")).not.toBeInTheDocument(),
  );
});

it("dismisses a suggestion", async () => {
  run = runWith({ suggestions: [peter] });
  const user = userEvent.setup();
  showDialog();
  await user.click(
    await screen.findByRole("button", { name: "Dismiss Peter for Speaker 2" }),
  );
  await waitFor(() =>
    expect(decided).toEqual([{ index: "0", decision: "dismiss" }]),
  );
});

it("says so when no names were found, and shows a failed run", async () => {
  run = runWith({});
  const first = showDialog();
  expect(
    await screen.findByText("No names were found in what was said."),
  ).toBeInTheDocument();
  first.onClose();

  run = runWith({ status: "failed", error: "The model returned no text." });
  showDialog();
  expect(
    await screen.findByText("The model returned no text."),
  ).toBeInTheDocument();
});

it("offers renaming only when no language model is configured", () => {
  showDialog({ canIdentify: false });
  expect(
    screen.queryByRole("button", { name: "Find names in the transcript" }),
  ).not.toBeInTheDocument();
  expect(
    within(screen.getByRole("dialog")).getByLabelText(/Speaker 2/),
  ).toBeInTheDocument();
});
