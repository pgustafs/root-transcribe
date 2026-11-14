import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it } from "vitest";
import type { MeetingRecordingDetail } from "@/api/types";
import { ToastProvider } from "@/components/toast/ToastProvider";
import i18n from "@/i18n";
import { server } from "@/test/msw/server";
import { WorkspaceMeetingsPage } from "./WorkspaceMeetingsPage";

const recording: MeetingRecordingDetail = {
  id: "r1",
  workspace_id: "w1",
  title: "Design interview",
  status: "completed",
  current_step: null,
  duration_seconds: 24,
  language: "en",
  size_bytes: 1024,
  error: null,
  error_code: null,
  participant_consent: true,
  audio_deleted: true,
  sensitivity: "internal",
  latest_revision_id: "v1",
  approved_revision_id: null,
  created_by_email: "demo@example.com",
  created_at: "2026-11-03T12:00:00Z",
  updated_at: "2026-11-03T12:00:00Z",
  legal_hold: false,
  segments: [
    "The first speaker opens the discussion.",
    "The second speaker answers.",
  ].map((text, idx) => ({
    id: `s${idx}`,
    revision_id: "v1",
    version: 1,
    idx,
    start_ms: idx * 10000,
    end_ms: idx * 10000 + 8000,
    speaker_label: `SPEAKER_0${idx}`,
    speaker_profile_id: null,
    speaker_name: null,
    speaker_confidence: null,
    text,
    is_edited: false,
  })),
};
const config = {
  project_name: "Root Transcribe",
  meetings_enabled: true,
  meetings_status: "available",
  meeting_consent_required: true,
  audio_max_bytes: 500 * 1024 * 1024,
  recording_max_minutes: 120,
};

function showPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/workspaces/w1/meetings"]}>
          <ToastProvider>
            <Routes>
              <Route
                path="/workspaces/:id/meetings"
                element={<WorkspaceMeetingsPage />}
              />
            </Routes>
          </ToastProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

beforeEach(() => {
  server.use(
    http.get("*/api/v1/config", () => HttpResponse.json(config)),
    http.get("*/api/v1/workspaces/w1", () =>
      HttpResponse.json({ id: "w1", name: "Interviews", my_role: "owner" }),
    ),
    http.get("*/api/v1/workspaces/w1/meetings/permissions", () =>
      HttpResponse.json({
        capabilities: [
          "record",
          "upload",
          "view_transcript",
          "listen",
          "export",
        ],
      }),
    ),
    http.get("*/api/v1/workspaces/w1/recordings", () =>
      HttpResponse.json([recording]),
    ),
    http.get("*/api/v1/workspaces/w1/recordings/r1", () =>
      HttpResponse.json(recording),
    ),
    http.get("*/api/v1/workspaces/w1/recordings/r1/speaker-suggestions", () =>
      HttpResponse.json([]),
    ),
    http.get("*/api/v1/workspaces/w1/recordings/r1/revisions", () =>
      HttpResponse.json([]),
    ),
  );
});

it("filters speaker turns and search together, and can clear an empty result", async () => {
  const actor = userEvent.setup();
  showPage();
  const speakers = await screen.findByRole("radiogroup", {
    name: "Filter transcript by speaker",
  });
  expect(screen.getByText(recording.segments[0].text)).toBeVisible();
  await actor.click(within(speakers).getByRole("radio", { name: /Speaker 2/ }));
  expect(
    screen.queryByText(recording.segments[0].text),
  ).not.toBeInTheDocument();
  expect(screen.getByText(recording.segments[1].text)).toBeVisible();
  await actor.type(
    screen.getByRole("textbox", { name: "Search transcript" }),
    "absent",
  );
  expect(
    screen.queryByText(recording.segments[1].text),
  ).not.toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: /clear all filters/i }));
  expect(screen.getByText(recording.segments[0].text)).toBeVisible();
  expect(screen.getByText(recording.segments[1].text)).toBeVisible();
  await actor.click(screen.getByRole("checkbox", { name: "Follow playback" }));
  expect(
    screen.getByRole("checkbox", { name: "Follow playback" }),
  ).not.toBeChecked();
});

it("keeps saved transcripts readable while preventing new uploads during an outage", async () => {
  server.use(
    http.get("*/api/v1/config", () =>
      HttpResponse.json({ ...config, meetings_status: "degraded" }),
    ),
  );
  showPage();
  expect(await screen.findByText(recording.segments[0].text)).toBeVisible();
  expect(screen.getByRole("button", { name: "Upload audio" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Record audio" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Check again" })).toBeEnabled();
});

it("explains a completed recording with no speech instead of showing a blank pane", async () => {
  server.use(
    http.get("*/api/v1/workspaces/w1/recordings/r1", () =>
      HttpResponse.json({ ...recording, segments: [] }),
    ),
  );
  showPage();
  expect(
    await screen.findByRole("heading", { name: "No speech found" }),
  ).toBeVisible();
  expect(
    screen.queryByRole("radiogroup", { name: "Filter transcript by speaker" }),
  ).not.toBeInTheDocument();
});

it("shows a retryable error when a selected transcript cannot be loaded", async () => {
  server.use(
    http.get("*/api/v1/workspaces/w1/recordings/r1", () =>
      HttpResponse.json({ detail: "Unavailable" }, { status: 503 }),
    ),
  );
  showPage();
  expect(
    await screen.findByRole("button", { name: /try again/i }),
  ).toBeEnabled();
  expect(
    screen.queryByText(/Transcribing your meeting/),
  ).not.toBeInTheDocument();
});

it("re-enables capture after an explicit availability refresh", async () => {
  const actor = userEvent.setup();
  let ready = false;
  server.use(
    http.get("*/api/v1/config", () =>
      HttpResponse.json({
        ...config,
        meetings_status: ready ? "available" : "degraded",
      }),
    ),
  );
  showPage();
  const check = await screen.findByRole("button", { name: "Check again" });
  expect(screen.getByRole("button", { name: "Upload audio" })).toBeDisabled();
  ready = true;
  await actor.click(check);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Upload audio" })).toBeEnabled(),
  );
});

it("keeps the original diarization label when assigning all turns to a person", async () => {
  const actor = userEvent.setup();
  let assignment: unknown;
  server.use(
    http.get("*/api/v1/workspaces/w1/meetings/permissions", () =>
      HttpResponse.json({
        capabilities: [
          "view_transcript",
          "relabel_speakers",
          "edit_transcript",
        ],
      }),
    ),
    http.get("*/api/v1/speakers", () =>
      HttpResponse.json([
        {
          id: "person1",
          display_name: "Alex",
          status: "active",
          voiceprint_count: 0,
        },
      ]),
    ),
    http.post(
      "*/api/v1/workspaces/w1/recordings/r1/assign-speaker",
      async ({ request }) => {
        assignment = await request.json();
        return HttpResponse.json({ updated_segments: 1 });
      },
    ),
  );
  showPage();
  await actor.click(
    await screen.findByRole("button", { name: "Assign Speaker 1" }),
  );
  expect(screen.getByText("All Speaker 1 turns")).toBeVisible();
  const choices = await screen.findAllByRole("menuitem", { name: "Alex" });
  await actor.click(choices[1]);
  await waitFor(() =>
    expect(assignment).toEqual({
      speaker_label: "SPEAKER_00",
      speaker_profile_id: "person1",
    }),
  );
});

it("shows native playback controls and lets a failed stream obtain a fresh session", async () => {
  const actor = userEvent.setup();
  let attempts = 0;
  server.use(
    http.get("*/api/v1/workspaces/w1/recordings/r1", () =>
      HttpResponse.json({ ...recording, audio_deleted: false }),
    ),
    http.post("*/api/v1/workspaces/w1/recordings/r1/audio/session", () => {
      attempts++;
      return HttpResponse.json({
        url: `/api/v1/audio/test-session-${attempts}`,
      });
    }),
  );
  showPage();
  const player = await screen.findByLabelText("Recording playback", {
    selector: "audio",
  });
  expect(player).toHaveAttribute("controls");
  expect(player).toHaveAttribute("src", "/api/v1/audio/test-session-1");
  const region = screen.getByRole("region", { name: "Recording playback" });
  expect(
    within(region).getByRole("button", { name: /skip.*back|back.*10/i }),
  ).toBeEnabled();
  // A media/network error must offer a way to refresh an expired stream URL.
  fireEvent.error(player);
  await actor.click(within(region).getByRole("button", { name: "Try again" }));
  await waitFor(() =>
    expect(
      screen.getByLabelText("Recording playback", { selector: "audio" }),
    ).toHaveAttribute("src", "/api/v1/audio/test-session-2"),
  );
});

it("switches review tabs by keyboard while preserving transcript filters", async () => {
  server.use(
    http.get("*/api/v1/workspaces/w1/recordings/r1/clips", () =>
      HttpResponse.json([]),
    ),
  );
  const actor = userEvent.setup();
  showPage();
  const transcript = await screen.findByRole("tab", { name: "Transcript" });
  await actor.type(
    screen.getByRole("textbox", { name: "Search transcript" }),
    "first",
  );
  transcript.focus();
  await actor.keyboard("{ArrowRight}");
  await waitFor(() =>
    expect(screen.getByRole("tab", { name: "Clips" })).toHaveAttribute(
      "aria-selected",
      "true",
    ),
  );
  expect(screen.getByRole("tabpanel", { name: "Clips" })).toBeVisible();
  expect(
    screen.queryByText(recording.segments[0].text),
  ).not.toBeInTheDocument();
  await actor.keyboard("{ArrowLeft}");
  await waitFor(() =>
    expect(transcript).toHaveAttribute("aria-selected", "true"),
  );
  expect(
    screen.getByRole("textbox", { name: "Search transcript" }),
  ).toHaveValue("first");
  expect(screen.getByText(recording.segments[0].text)).toBeVisible();
  expect(
    screen.queryByText(recording.segments[1].text),
  ).not.toBeInTheDocument();
});

it.each([false, true])(
  "asks for recording language before upload (consent required: %s)",
  async (consentRequired) => {
    let language: unknown;
    let consent: unknown;
    server.use(
      http.get("*/api/v1/config", () =>
        HttpResponse.json({
          ...config,
          meeting_consent_required: consentRequired,
        }),
      ),
      http.post("*/api/v1/workspaces/w1/recordings", async ({ request }) => {
        const data = await request.formData();
        language = data.get("requested_language");
        consent = data.get("participant_consent");
        return HttpResponse.json(recording, { status: 202 });
      }),
    );
    showPage();
    await screen.findByText(recording.segments[0].text);
    const user = userEvent.setup();
    await user.upload(
      screen.getByLabelText("Upload audio"),
      new File([new Uint8Array(256)], "interview.wav", { type: "audio/wav" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Upload audio" });
    expect(language).toBeUndefined(); // selecting a file must not start the upload
    await user.click(within(dialog).getByRole("radio", { name: "English" }));
    if (consentRequired) {
      expect(
        within(dialog).getByRole("button", { name: "Upload audio" }),
      ).toBeDisabled();
      await user.click(within(dialog).getByRole("checkbox"));
    }
    await user.click(
      within(dialog).getByRole("button", { name: "Upload audio" }),
    );
    await waitFor(() => expect(language).toBe("en"));
    expect(consent).toBe(String(consentRequired));
  },
);
