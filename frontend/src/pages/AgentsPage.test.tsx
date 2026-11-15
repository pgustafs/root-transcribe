import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it } from "vitest";

import type { NoteAgent } from "@/api/notes";
import type { User } from "@/api/types";
import { AuthContext } from "@/auth/authContext";
import { ToastProvider } from "@/components/toast/ToastProvider";
import { TooltipProvider } from "@/components/ui/tooltip";
import i18n from "@/i18n";
import { server } from "@/test/msw/server";

import { AgentsPage } from "./AgentsPage";

const user = (role: User["role"]): User => ({
  id: "u1",
  email: "someone@example.com",
  full_name: "Someone",
  avatar_url: null,
  role,
  is_active: true,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

const agent = (over: Partial<NoteAgent>): NoteAgent => ({
  id: "a1",
  name: "Summary",
  description: "What the meeting was about",
  instructions: "Write a summary.",
  is_active: true,
  shared: true,
  builtin: true,
  modified: false,
  can_edit: false,
  updated_at: "2026-11-14T10:00:00Z",
  ...over,
});

let agents: NoteAgent[];

/** The page for a signed-in user of the given role, with a fresh data cache. */
function showPage(role: User["role"]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const auth = {
    user: user(role),
    isLoading: false,
    isAuthenticated: true,
    isAdmin: role === "admin",
    isPrivileged: role === "admin" || role === "power_user",
    login: async () => {},
    logout: () => {},
    refreshUser: async () => {},
  };
  render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <AuthContext.Provider value={auth}>
          <MemoryRouter>
            <TooltipProvider>
              <ToastProvider>
                <AgentsPage />
              </ToastProvider>
            </TooltipProvider>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

beforeEach(() => {
  agents = [agent({})];
  server.use(
    http.get("*/api/v1/config", () =>
      HttpResponse.json({
        project_name: "Root Transcribe",
        meetings_enabled: true,
        meetings_status: "available",
        meeting_consent_required: true,
        notes_status: "available",
      }),
    ),
    http.get("*/api/v1/note-agents", () => HttpResponse.json(agents)),
  );
});

it("lets any user add a personal agent but only read shared ones", async () => {
  const actor = userEvent.setup();
  let created: unknown;
  server.use(
    http.post("*/api/v1/note-agents", async ({ request }) => {
      created = await request.json();
      const mine = agent({
        id: "a9",
        name: "Decisions",
        shared: false,
        builtin: false,
        can_edit: true,
      });
      agents = [...agents, mine];
      return HttpResponse.json(mine, { status: 201 });
    }),
  );
  showPage("user");
  expect(await screen.findByRole("heading", { name: "Agents" })).toBeVisible();
  expect(
    await screen.findByText("You have no agents of your own"),
  ).toBeVisible();
  // A normal user sees the shared agent but cannot switch it or add shared ones.
  expect(
    screen.getByRole("switch", {
      name: "Summary is available when creating documents",
    }),
  ).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "New shared agent" }),
  ).not.toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: "Summary" }));
  let dialog = await screen.findByRole("dialog", { name: "Summary" });
  expect(within(dialog).getByText("Write a summary.")).toBeVisible();
  expect(
    within(dialog).queryByRole("button", { name: "Save" }),
  ).not.toBeInTheDocument();
  // (the dialog also has its corner close button)
  await actor.click(
    within(dialog).getAllByRole("button", { name: "Close" })[0],
  );

  await actor.click(screen.getByRole("button", { name: "New agent" }));
  dialog = await screen.findByRole("dialog", { name: "New agent" });
  // Empty form: both required fields are reported, nothing is sent.
  await actor.click(within(dialog).getByRole("button", { name: "Create" }));
  expect(within(dialog).getByText("Give the agent a name")).toBeVisible();
  expect(created).toBeUndefined();
  await actor.type(
    within(dialog).getByRole("textbox", { name: "Name" }),
    "Decisions",
  );
  await actor.type(
    within(dialog).getByRole("textbox", { name: "Instructions" }),
    "List the decisions.",
  );
  await actor.click(within(dialog).getByRole("button", { name: "Create" }));
  await waitFor(() =>
    expect(created).toEqual({
      name: "Decisions",
      description: null,
      instructions: "List the decisions.",
      shared: false,
    }),
  );
  expect(
    await screen.findByRole("button", { name: "Decisions" }),
  ).toBeVisible();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("lets a power user edit, switch off and reset a shared built-in", async () => {
  agents = [agent({ can_edit: true, modified: true })];
  const actor = userEvent.setup();
  const patches: unknown[] = [];
  let wasReset = false;
  server.use(
    http.patch("*/api/v1/note-agents/a1", async ({ request }) => {
      const body = (await request.json()) as Partial<NoteAgent>;
      patches.push(body);
      agents = [{ ...agents[0], ...body }];
      return HttpResponse.json(agents[0]);
    }),
    http.post("*/api/v1/note-agents/a1/reset", () => {
      wasReset = true;
      agents = [agent({ can_edit: true })];
      return HttpResponse.json(agents[0]);
    }),
  );
  showPage("power_user");
  expect(
    await screen.findByRole("button", { name: "New shared agent" }),
  ).toBeVisible();
  expect(await screen.findByText("Changed from default")).toBeVisible();
  await actor.click(
    screen.getByRole("switch", {
      name: "Summary is available when creating documents",
    }),
  );
  await waitFor(() => expect(patches).toEqual([{ is_active: false }]));

  await actor.click(screen.getByRole("button", { name: "Summary" }));
  let dialog = await screen.findByRole("dialog", { name: "Edit agent" });
  const instructions = within(dialog).getByRole("textbox", {
    name: "Instructions",
  });
  await actor.clear(instructions);
  await actor.type(instructions, "Be brief.");
  await actor.click(within(dialog).getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(patches[1]).toEqual({
      name: "Summary",
      description: "What the meeting was about",
      instructions: "Be brief.",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );

  await actor.click(screen.getByRole("button", { name: "Summary" }));
  dialog = await screen.findByRole("dialog", { name: "Edit agent" });
  await actor.click(
    within(dialog).getByRole("button", { name: "Reset to default" }),
  );
  await waitFor(() => expect(wasReset).toBe(true));
  await waitFor(() =>
    expect(screen.queryByText("Changed from default")).not.toBeInTheDocument(),
  );
  // Built-ins can be switched off but never deleted.
  await actor.click(
    screen.getByRole("button", { name: "Actions for Summary" }),
  );
  expect(
    screen.queryByRole("menuitem", { name: "Delete" }),
  ).not.toBeInTheDocument();
});
