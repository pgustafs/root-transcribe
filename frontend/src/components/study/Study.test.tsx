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

const actions = () => ({
  onToggle: vi.fn(),
  onMark: vi.fn(),
  onThought: vi.fn(),
  onKeepOffer: vi.fn(),
});

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
    actions: actions(),
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
  expect(study.actions.onToggle).toHaveBeenCalledOnce();
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
  const given = actions();
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
      actions={given}
    />,
  );
  return given;
}

it("floating window: one focused button that takes and stops a note", async () => {
  const given = float({ count: 2 });
  const take = screen.getByRole("button", { name: "Take note" });
  expect(take).toHaveFocus();
  expect(screen.getByText("2 notes")).toBeInTheDocument();
  await userEvent.keyboard("{Enter}");
  expect(given.onToggle).toHaveBeenCalledOnce();
});

it("floating window: marks, a thought and the slide offer", async () => {
  const given = float({
    openSinceMs: 522_000,
    openTags: ["important"],
    offer: { id: "o1", capturedMs: 500_000 },
  });
  // Marks show their state on the open note.
  expect(screen.getByRole("button", { name: "Important" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  const unclear = screen.getByRole("button", { name: "Don't understand" });
  expect(unclear).toHaveAttribute("aria-pressed", "false");
  // A typed thought goes with the next button that is pressed.
  const field = screen.getByRole("textbox", {
    name: "A thought for this note",
  });
  await userEvent.type(field, "Why not a full scan?");
  await userEvent.click(unclear);
  expect(given.onMark).toHaveBeenCalledWith("unclear", "Why not a full scan?");
  expect(field).toHaveValue("");
  // Enter saves a thought on its own.
  await userEvent.type(field, "Ask on Friday{Enter}");
  expect(given.onThought).toHaveBeenCalledWith("Ask on Friday");
  await userEvent.click(
    screen.getByRole("button", { name: "Keep previous slide" }),
  );
  expect(given.onKeepOffer).toHaveBeenCalledOnce();
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
  tags: [],
  title: null,
  clean_text: null,
  slide_text: null,
  terms: null,
  explanation: null,
  enrich_status: "none",
  enrich_error: null,
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
    http.get(`${API}/workspaces/w1/study/features`, () =>
      HttpResponse.json({ enrich: true }),
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

it("notebook: what the model added, marks, search, glossary and wider context", async () => {
  const patched: unknown[] = [];
  server.use(
    http.patch(`${API}/workspaces/w1/study/notes/n1`, async ({ request }) => {
      const body = (await request.json()) as { tags: StudyNote["tags"] };
      patched.push(body);
      return HttpResponse.json(
        note(1, { text: "Joins come next.", tags: body.tags }),
      );
    }),
    http.get(`${API}/workspaces/w1/study/notes/n0/context`, ({ request }) => {
      const seconds = Number(new URL(request.url).searchParams.get("seconds"));
      return HttpResponse.json({
        available: true,
        reason: null,
        start_ms: 507_000 - seconds * 1000,
        end_ms: 543_000 + seconds * 1000,
        at_start: false,
        at_end: false,
        segments: [
          {
            start_ms: 480_000,
            end_ms: 500_000,
            speaker: "Anna",
            text: "Before that we looked at full scans.",
          },
        ],
      });
    }),
  );
  notebookPage([
    note(0, {
      title: "Index scan",
      text: "so um the database can use this index",
      clean_text: "The database can use this index.",
      slide_text: "# Query plan\n\n`SELECT 1;`",
      terms: [{ term: "Index", definition: "A lookup structure." }],
      explanation: "It looks things up instead of reading everything.",
      tags: ["unclear"],
      enrich_status: "ready",
    }),
    note(1, { text: "Joins come next.", enrich_status: "pending" }),
  ]);
  const entries = await screen.findAllByRole("article");
  const first = within(entries[0]);
  // The generated title is the heading; the time moves to the line below.
  expect(
    first.getByRole("heading", { name: "Index scan" }),
  ).toBeInTheDocument();
  expect(first.getByText(/At 08:42 in this recording/)).toBeInTheDocument();
  expect(first.getByText("Don't understand")).toBeInTheDocument();
  expect(
    first.getByText("It looks things up instead of reading everything."),
  ).toBeInTheDocument();
  expect(first.getByText("A lookup structure.")).toBeInTheDocument();
  // Tidy text first; what was actually said is one click away.
  expect(
    first.getByText("The database can use this index."),
  ).toBeInTheDocument();
  await userEvent.click(first.getByRole("button", { name: "Show original" }));
  expect(
    first.getByText("so um the database can use this index"),
  ).toBeInTheDocument();
  // The slide text is folded away until asked for.
  expect(first.queryByText("Query plan")).not.toBeInTheDocument();
  await userEvent.click(
    first.getByRole("button", { name: "Text on the slide" }),
  );
  expect(
    first.getByRole("heading", { name: "Query plan" }),
  ).toBeInTheDocument();
  // Wider context comes from the full recording.
  await userEvent.click(
    first.getByRole("button", { name: "Show 30 seconds more" }),
  );
  expect(
    await first.findByText("Before that we looked at full scans."),
  ).toBeInTheDocument();
  expect(
    first.getByText("From the full recording, 07:57–09:33"),
  ).toBeInTheDocument();
  // The second note is still being worked on, and can be marked by hand.
  const second = within(entries[1]);
  expect(second.getByText("Improving the note…")).toBeInTheDocument();
  await userEvent.click(
    second.getByRole("button", { name: /More actions for the note/ }),
  );
  await userEvent.click(
    await screen.findByRole("menuitemcheckbox", { name: "Important" }),
  );
  await waitFor(() => expect(patched).toEqual([{ tags: ["important"] }]));

  // Search looks in the slide text too; the filter narrows by mark.
  await userEvent.type(
    screen.getByRole("textbox", { name: "Search notes" }),
    "select",
  );
  expect(screen.getAllByRole("article")).toHaveLength(1);
  await userEvent.clear(screen.getByRole("textbox", { name: "Search notes" }));
  await userEvent.click(screen.getByRole("radio", { name: "Important" }));
  await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(1));
  expect(
    within(screen.getByRole("article")).getByText("Joins come next."),
  ).toBeInTheDocument();
  // The glossary lists each term once and leads back to its note.
  await userEvent.click(screen.getByRole("tab", { name: "Glossary (1)" }));
  expect(await screen.findByText("Index")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Index scan" }));
  expect(await screen.findAllByRole("article")).toHaveLength(2);
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
