import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import type { StudyNote, StudyNotebookDetail } from "@/api/study";
import {
  RecordingDock,
  type StudyDockProps,
} from "@/components/recording/RecordingDock";
import { RecordingProvider } from "@/components/recording/RecordingProvider";
import { FloatingNotePanel } from "@/components/study/FloatingNotePanel";
import { StudyCropStep } from "@/components/study/StudyCropStep";
import { ToastProvider } from "@/components/toast/ToastProvider";
import i18n from "@/i18n";
import { IDLE_STUDY, type StudyStatus } from "@/lib/study/noteController";
import { StudyNotesPage } from "@/pages/StudyNotesPage";
import { server } from "@/test/msw/server";

const API = "*/api/v1";

function wrap(ui: ReactNode, route = "/") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>
          <ToastProvider>{ui}</ToastProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  server.use(
    http.get(`${API}/workspaces/:id`, () =>
      HttpResponse.json({ id: "w1", name: "Course", description: null }),
    ),
  );
});

// --- the dock -------------------------------------------------------------------

function dock(
  status: Partial<StudyStatus>,
  over: Partial<StudyDockProps> = {},
) {
  const study: StudyDockProps = {
    status: {
      ...IDLE_STUDY,
      active: true,
      notebookTitle: "Query plans",
      audioAvailable: true,
      videoAvailable: true,
      ...status,
    },
    onToggle: vi.fn(),
    onViewNotes: vi.fn(),
    onChangeSource: vi.fn(),
    ...over,
  };
  wrap(
    <RecordingDock
      status="recording"
      title="Query plans"
      durationMs={543_000}
      level={0.2}
      showLevel
      trackWarning={null}
      noTabAudio={false}
      uploadProgress={null}
      uploadError={null}
      storageDegraded={false}
      onStop={vi.fn()}
      onRetry={vi.fn()}
      onPause={vi.fn()}
      onResume={vi.fn()}
      onCancelRequest={vi.fn()}
      busy={false}
      study={study}
    />,
  );
  return study;
}

it("dock: takes a note and keeps the recording controls", async () => {
  const study = dock({ count: 3 });
  await userEvent.click(screen.getByRole("button", { name: "Take note" }));
  expect(study.onToggle).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "View 3 notes" })).toBeEnabled();
  for (const name of ["Pause", "Stop & transcribe", "Cancel", "Change source"])
    expect(screen.getByRole("button", { name })).toBeEnabled();
  // Where the browser has no floating windows, the button is not offered.
  expect(
    screen.queryByRole("button", { name: "Show floating button" }),
  ).not.toBeInTheDocument();
});

it("dock: shows the open note's length and opens the floating button", async () => {
  const onToggle = vi.fn();
  dock(
    { openSinceMs: 522_000, count: 1 },
    { floating: { isOpen: false, onToggle } },
  );
  expect(screen.getByRole("button", { name: "Stop note" })).toBeEnabled();
  expect(screen.getByText("Capturing note · 00:21")).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Show floating button" }),
  );
  expect(onToggle).toHaveBeenCalledOnce();
});

it("dock: explains why no note can be taken", () => {
  dock({ videoAvailable: false, audioAvailable: false });
  expect(screen.getByRole("button", { name: "Take note" })).toBeDisabled();
  expect(
    screen.getByText(/shared tab is no longer available/),
  ).toBeInTheDocument();
});

it("dock: says when notes are screenshots only", () => {
  dock({ audioAvailable: false });
  expect(screen.getByRole("button", { name: "Take note" })).toBeEnabled();
  expect(
    screen.getByText("Screenshot only · Tab audio unavailable"),
  ).toBeInTheDocument();
  expect(screen.getByText(/Also share tab audio/)).toBeInTheDocument();
});

// --- the floating window --------------------------------------------------------

function float(status: Partial<StudyStatus>, over: { paused?: boolean } = {}) {
  const onToggle = vi.fn();
  wrap(
    <FloatingNotePanel
      status={{
        ...IDLE_STUDY,
        active: true,
        audioAvailable: true,
        videoAvailable: true,
        ...status,
      }}
      recording={!over.paused}
      paused={Boolean(over.paused)}
      durationMs={543_000}
      onToggle={onToggle}
    />,
  );
  return onToggle;
}

it("floating window: one focused button that takes and stops a note", async () => {
  const onToggle = float({ count: 2 });
  const take = screen.getByRole("button", { name: "Take note" });
  expect(take).toHaveFocus();
  expect(screen.getByText("2 notes")).toBeInTheDocument();
  await userEvent.keyboard("{Enter}");
  expect(onToggle).toHaveBeenCalledOnce();
});

it("floating window: shows the open note's length", () => {
  float({ openSinceMs: 522_000, count: 1 });
  expect(screen.getByRole("button", { name: "Stop note" })).toBeEnabled();
  expect(screen.getByText("Capturing note · 00:21")).toBeInTheDocument();
});

it("floating window: says why no note can be taken", () => {
  float({}, { paused: true });
  expect(screen.getByRole("button", { name: "Take note" })).toBeDisabled();
  expect(
    screen.getByText("Resume the recording to take notes."),
  ).toBeInTheDocument();
});

// --- crop -----------------------------------------------------------------------

it("crop step: the video area can be set from the keyboard", async () => {
  const onChange = vi.fn();
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
  wrap(
    <StudyCropStep
      getPreview={async () => new Blob(["x"], { type: "image/png" })}
      initial={{ x: 0, y: 0, width: 1, height: 1 }}
      onChange={onChange}
    />,
  );
  await screen.findByAltText("Preview of the shared tab");
  await userEvent.click(
    screen.getByRole("radio", { name: "Select video area" }),
  );
  const area = await screen.findByRole("button", { name: "Video area" });
  area.focus();
  await userEvent.keyboard(
    "{ArrowRight}{ArrowDown}{Shift>}{ArrowLeft}{/Shift}",
  );
  expect(onChange).toHaveBeenLastCalledWith({
    x: 0.06,
    y: 0.11,
    width: 0.64,
    height: 0.65,
  });
  const left = screen.getByLabelText("Left %");
  await userEvent.clear(left);
  await userEvent.type(left, "20");
  expect(onChange.mock.lastCall?.[0].x).toBe(0.2);
  await userEvent.click(screen.getByRole("radio", { name: "Use full frame" }));
  expect(onChange).toHaveBeenLastCalledWith({
    x: 0,
    y: 0,
    width: 1,
    height: 1,
  });
});

// --- the notebook page ------------------------------------------------------------

const note = (seq: number, over: Partial<StudyNote>): StudyNote => ({
  id: `n${seq}`,
  notebook_id: "nb1",
  session_id: "s1",
  seq,
  captured_ms: 522_000 + seq * 60_000,
  audio_start_ms: 507_000 + seq * 60_000,
  audio_end_ms: 543_000 + seq * 60_000,
  has_image: true,
  image_width: 640,
  image_height: 360,
  has_audio: true,
  status: "ready",
  text: "The database can use this index.",
  error: null,
  comment: null,
  end_reason: null,
  source_time_url: null,
  created_at: "2026-11-15T10:00:00Z",
  ...over,
});

function notebookPage(notes: StudyNote[]) {
  const detail: StudyNotebookDetail = {
    id: "nb1",
    workspace_id: "w1",
    title: "Query plans",
    source_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    source_key: "youtube:dQw4w9WgXcQ",
    note_count: notes.length,
    created_at: "2026-11-15T10:00:00Z",
    updated_at: "2026-11-15T10:00:00Z",
    sessions: [
      {
        id: "s1",
        notebook_id: "nb1",
        recording_id: null,
        language: "auto",
        mode: "until_stopped",
        started_at: "2026-11-15T10:00:00Z",
      },
    ],
    notes,
  };
  server.use(
    http.get(`${API}/workspaces/w1/study/notebooks/nb1`, () =>
      HttpResponse.json(detail),
    ),
    http.get(`${API}/workspaces/w1/study/notes/:id/image`, () =>
      HttpResponse.arrayBuffer(new Uint8Array([1, 2, 3]).buffer, {
        headers: { "Content-Type": "image/png" },
      }),
    ),
  );
  URL.createObjectURL = vi.fn(() => "blob:image");
  URL.revokeObjectURL = vi.fn();
  wrap(
    <RecordingProvider>
      <Routes>
        <Route
          path="/workspaces/:id/study/:notebookId"
          element={<StudyNotesPage />}
        />
      </Routes>
    </RecordingProvider>,
    "/workspaces/w1/study/nb1",
  );
}

it("notebook: entries in capture order, each with a text state", async () => {
  const retried = vi.fn();
  server.use(
    http.post(`${API}/workspaces/w1/study/notes/n2/retry`, () => {
      retried();
      return HttpResponse.json(note(2, { status: "pending", text: null }));
    }),
  );
  notebookPage([
    note(0, {}),
    note(1, { status: "pending", text: null }),
    note(2, { status: "failed", text: null }),
    note(3, {
      status: "screenshot_only",
      text: null,
      has_audio: false,
      audio_start_ms: null,
      audio_end_ms: null,
    }),
    note(4, { status: "no_speech", text: null }),
  ]);
  expect(
    await screen.findByRole("heading", { name: "Query plans", level: 1 }),
  ).toBeInTheDocument();
  expect(screen.getByText("5 notes")).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "Recorded November 15, 2026" }),
  ).toBeInTheDocument();
  const entries = screen.getAllByRole("article");
  expect(entries).toHaveLength(5);
  const first = within(entries[0]);
  expect(
    first.getByRole("heading", { name: "At 08:42 in this recording" }),
  ).toBeInTheDocument();
  expect(first.getByText("Audio 08:27–09:03")).toBeInTheDocument();
  expect(
    first.getByText("The database can use this index."),
  ).toBeInTheDocument();
  expect(
    await first.findByRole("button", {
      name: "View image: Query plans, captured at 08:42",
    }),
  ).toBeInTheDocument();
  expect(within(entries[1]).getByText("Transcribing…")).toBeInTheDocument();
  expect(
    within(entries[2]).getByText("Transcription failed"),
  ).toBeInTheDocument();
  expect(
    within(entries[3]).getByText("Screenshot only · Tab audio unavailable"),
  ).toBeInTheDocument();
  expect(
    within(entries[4]).getByText("No speech detected"),
  ).toBeInTheDocument();
  // The failed entry keeps its image and can be tried again.
  expect(
    await within(entries[2]).findByRole("button", {
      name: "View image: Query plans, captured at 10:42",
    }),
  ).toBeInTheDocument();
  await userEvent.click(
    within(entries[2]).getByRole("button", { name: "Retry" }),
  );
  await waitFor(() => expect(retried).toHaveBeenCalledOnce());
});

it("notebook: own thoughts are saved apart from the transcript", async () => {
  const sent: unknown[] = [];
  server.use(
    http.patch(`${API}/workspaces/w1/study/notes/n0`, async ({ request }) => {
      const body = (await request.json()) as { comment: string };
      sent.push(body);
      return HttpResponse.json(note(0, { comment: body.comment }));
    }),
  );
  notebookPage([note(0, {}), note(1, { comment: "Ask about this." })]);
  const fields = await screen.findAllByRole("textbox", { name: "My thoughts" });
  expect(fields[1]).toHaveValue("Ask about this.");
  await userEvent.type(fields[0], "Compare with a full scan.");
  await userEvent.tab();
  expect(await screen.findByText("Saved")).toBeInTheDocument();
  expect(sent).toEqual([{ comment: "Compare with a full scan." }]);
  // The transcript is untouched and still shown as the quote.
  expect(
    screen.getAllByText("The database can use this index.")[0].tagName,
  ).toBe("BLOCKQUOTE");
  // Leaving the field without a change sends nothing.
  await userEvent.click(fields[1]);
  await userEvent.tab();
  expect(sent).toHaveLength(1);
});

it("notebook: a refused export says why", async () => {
  server.use(
    http.get(`${API}/workspaces/w1/study/notebooks/nb1/export`, () =>
      HttpResponse.json({ detail: "restricted" }, { status: 403 }),
    ),
  );
  notebookPage([note(0, {})]);
  await userEvent.click(
    await screen.findByRole("button", { name: "Export Markdown" }),
  );
  expect(
    await screen.findByText(/A recording in this notebook is restricted/),
  ).toBeInTheDocument();
});
