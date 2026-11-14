import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it } from "vitest";

import type { MeetingNote, NoteAgent } from "@/api/notes";
import { ToastProvider } from "@/components/toast/ToastProvider";
import { TooltipProvider } from "@/components/ui/tooltip";
import i18n from "@/i18n";
import { server } from "@/test/msw/server";

import { MeetingNotesPanel } from "./MeetingNotesPanel";

const NOTES = "*/api/v1/workspaces/w1/recordings/r1/notes";

const agent = (over: Partial<NoteAgent>): NoteAgent => ({
  id: "a1",
  name: "Summary",
  description: "What the meeting was about",
  instructions: "Summarize.",
  is_active: true,
  shared: true,
  builtin: true,
  modified: false,
  can_edit: false,
  updated_at: "2026-11-14T10:00:00Z",
  ...over,
});

const note = (over: Partial<MeetingNote>): MeetingNote => ({
  id: "n1",
  recording_id: "r1",
  agent_id: "a1",
  agent_name: "Summary",
  revision_id: "v1",
  status: "ready",
  current_step: null,
  content_markdown:
    "## Summary\n\nAnna explained the **pilot**.\n\n- [ ] Erik sends the plan",
  version: 1,
  edited: false,
  edited_by_email: null,
  edited_at: null,
  error: null,
  model: "test-model",
  duration_ms: 1200,
  created_by_email: "demo@example.com",
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  ...over,
});

let notes: MeetingNote[];

function showPanel(
  props: Partial<Parameters<typeof MeetingNotesPanel>[0]> = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <TooltipProvider>
            <ToastProvider>
              <MeetingNotesPanel
                workspaceId="w1"
                recordingId="r1"
                displayRevisionId="v1"
                canCreate={true}
                canExport={true}
                isAvailable={true}
                {...props}
              />
            </ToastProvider>
          </TooltipProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

beforeEach(() => {
  notes = [];
  server.use(
    http.get("*/api/v1/note-agents", () =>
      HttpResponse.json([
        agent({}),
        agent({ id: "a2", name: "Task list" }),
        agent({ id: "a3", name: "Off", is_active: false }),
        agent({ id: "a4", name: "My style", shared: false, builtin: false }),
      ]),
    ),
    http.get(NOTES, () => HttpResponse.json(notes)),
  );
});

it("runs an agent and shows the note it wrote", async () => {
  const actor = userEvent.setup();
  let requested: unknown;
  server.use(
    http.post(NOTES, async ({ request }) => {
      requested = await request.json();
      // The agent finishes straight away: the next poll returns the document.
      notes = [note({ agent_id: "a2", agent_name: "Task list" })];
      return HttpResponse.json(
        note({ status: "pending", content_markdown: null, agent_id: "a2" }),
        { status: 202 },
      );
    }),
  );
  showPanel();
  expect(await screen.findByText("No notes yet")).toBeVisible();
  // Switched-off agents are not offered; personal ones are grouped apart.
  expect(screen.queryByRole("button", { name: "Off" })).not.toBeInTheDocument();
  expect(screen.getByText("My agents")).toBeVisible();
  expect(screen.getByRole("link", { name: "Manage agents" })).toHaveAttribute(
    "href",
    "/agents",
  );
  await actor.click(screen.getByRole("button", { name: "Task list" }));
  await waitFor(() => expect(requested).toEqual({ agent_id: "a2" }));
  const card = await screen.findByRole("article", { name: "Note: Task list" });
  // Rendered as a formatted document, not raw Markdown.
  expect(
    within(card).getByRole("heading", { name: "Summary", level: 2 }),
  ).toBeVisible();
  expect(within(card).getByText("pilot").tagName).toBe("STRONG");
  expect(within(card).getByRole("checkbox")).not.toBeChecked();
  expect(within(card).queryByText(/\*\*pilot\*\*/)).not.toBeInTheDocument();
});

it("shows progress while the agent works and the reason when it fails", async () => {
  notes = [
    note({ id: "n2", status: "processing", current_step: "part:2/4" }),
    note({
      id: "n3",
      agent_id: "a2",
      agent_name: "Task list",
      status: "failed",
      content_markdown: null,
      error: "The agent service could not be reached.",
    }),
  ];
  showPanel();
  const working = await screen.findByRole("article", { name: "Note: Summary" });
  expect(working).toHaveAttribute("aria-busy", "true");
  expect(
    within(working).getByText("Long meeting: working on part 2 of 4…"),
  ).toBeVisible();
  // The same agent cannot be started twice while it is running.
  expect(screen.getByRole("button", { name: "Summary" })).toBeDisabled();
  const failed = screen.getByRole("article", { name: "Note: Task list" });
  expect(
    within(failed).getByText("The agent service could not be reached."),
  ).toBeVisible();
  expect(
    within(failed).getByRole("button", { name: "Try again" }),
  ).toBeEnabled();
});

it("edits a note with the Markdown editor and saves it", async () => {
  const actor = userEvent.setup();
  notes = [note({})];
  let saved: unknown;
  server.use(
    http.patch(`${NOTES}/n1`, async ({ request }) => {
      saved = await request.json();
      notes = [
        note({ content_markdown: "## Changed", version: 2, edited: true }),
      ];
      return HttpResponse.json(notes[0]);
    }),
  );
  showPanel();
  const card = await screen.findByRole("article", { name: "Note: Summary" });
  await actor.click(within(card).getByRole("button", { name: "Edit" }));
  const editor = within(card).getByRole("textbox", {
    name: "Edit note: Summary",
  });
  expect(editor).toHaveValue(notes[0].content_markdown);
  expect(within(card).getByRole("button", { name: "Save" })).toBeDisabled();
  await actor.clear(editor);
  await actor.type(editor, "## Changed");
  // Preview shows the formatted result before saving.
  await actor.click(within(card).getByRole("tab", { name: "Preview" }));
  expect(
    within(card).getByRole("heading", { name: "Changed", level: 2 }),
  ).toBeVisible();
  await actor.click(within(card).getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(saved).toEqual({
      content_markdown: "## Changed",
      expected_version: 1,
    }),
  );
  expect(await within(card).findByText("Edited")).toBeVisible();
  expect(
    within(card).queryByRole("textbox", { name: "Edit note: Summary" }),
  ).not.toBeInTheDocument();
});

it("keeps earlier versions folded and flags notes from an older transcript", async () => {
  const actor = userEvent.setup();
  notes = [
    note({ id: "new", content_markdown: "Newest text" }),
    note({ id: "old", content_markdown: "Older text", revision_id: "v0" }),
  ];
  showPanel();
  expect(await screen.findByText("Newest text")).toBeVisible();
  expect(screen.queryByText("Older text")).not.toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: "1 earlier version" }));
  expect(await screen.findByText("Older text")).toBeVisible();
  expect(
    screen.getByText(/The transcript has changed since this note was written/),
  ).toBeVisible();
});

it("is read-only without the permission and explains a missing model", async () => {
  notes = [note({})];
  showPanel({ canCreate: false, canExport: false, isAvailable: false });
  const card = await screen.findByRole("article", { name: "Note: Summary" });
  expect(screen.getByText("Notes cannot be created right now")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Task list" }),
  ).not.toBeInTheDocument();
  expect(within(card).getByRole("button", { name: "Copy" })).toBeEnabled();
  for (const name of ["Edit", "Export"])
    expect(
      within(card).queryByRole("button", { name }),
    ).not.toBeInTheDocument();
});
