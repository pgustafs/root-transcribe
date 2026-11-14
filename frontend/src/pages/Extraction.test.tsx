import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import { App } from "@/App";
import { identitySession } from "@/test/identity";
import { server } from "@/test/msw/server";
import type { User, Workspace } from "@/api/types";

const user: User = {
  id: "u1",
  email: "reviewer@example.com",
  full_name: "Reviewer",
  avatar_url: null,
  role: "admin",
  is_active: true,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};
const workspace: Workspace = {
  id: "w1",
  name: "Interview",
  description: null,
  visibility: "private",
  owner_id: user.id,
  created_by_id: user.id,
  member_count: 0,
  my_role: "owner",
  created_at: "2026-01-01T00:00:00Z",
};
beforeEach(() => {
  window.history.replaceState({}, "", "/workspaces");
  identitySession.authenticated = true;
  identitySession.token = "test-access-token";
  server.use(
    http.get("*/api/v1/config", () =>
      HttpResponse.json({
        project_name: "Root Transcribe",
        meetings_enabled: true,
        meetings_status: "available",
        meeting_consent_required: true,
      }),
    ),
    http.get("*/api/v1/users/me", () => HttpResponse.json(user)),
    http.get("*/api/v1/notifications", () => HttpResponse.json([])),
    http.get("*/api/v1/notifications/unread-count", () =>
      HttpResponse.json({ count: 0 }),
    ),
    http.get("*/api/v1/workspaces/mine", () => HttpResponse.json([])),
    http.get("*/api/v1/workspaces/w1", () => HttpResponse.json(workspace)),
    http.get("*/api/v1/workspaces/w1/recordings", () => HttpResponse.json([])),
    http.get("*/api/v1/workspaces/w1/meetings/permissions", () =>
      HttpResponse.json({
        capabilities: ["record", "upload", "view_transcript"],
      }),
    ),
  );
});

it("creates a recording workspace through the copied form and opens transcription", async () => {
  const actor = userEvent.setup();
  let submitted: unknown;
  server.use(
    http.post("*/api/v1/workspaces", async ({ request }) => {
      submitted = await request.json();
      return HttpResponse.json(workspace, { status: 201 });
    }),
  );
  render(<App />);
  await actor.click(
    await screen.findByRole("button", { name: "New workspace" }),
  );
  await actor.type(screen.getByRole("textbox", { name: /name/i }), "Interview");
  await actor.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() =>
    expect(window.location.pathname).toBe("/workspaces/w1/meetings"),
  );
  expect(submitted).toEqual({ name: "Interview", description: null });
  expect(
    await screen.findByRole("button", { name: "Upload audio" }),
  ).toBeEnabled();
  expect(
    screen.queryByRole("link", { name: "Assistant" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("link", { name: "Knowledge bases" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /save as source/i }),
  ).not.toBeInTheDocument();
});

it("returns an authenticated sign-in visit to recording workspaces", async () => {
  window.history.replaceState({}, "", "/login");
  render(<App />);
  await waitFor(() => expect(window.location.pathname).toBe("/workspaces"));
  expect(
    (await screen.findAllByRole("button", { name: "New workspace" }))[0],
  ).toBeVisible();
});

it("opens account and notification menus and keeps the theme toggle synchronized", async () => {
  const actor = userEvent.setup();
  render(<App />);
  await actor.click(await screen.findByRole("button", { name: "Theme" }));
  await actor.click(await screen.findByRole("menuitemradio", { name: "Dark" }));
  expect(document.documentElement).toHaveClass("dark");
  expect(localStorage.getItem("root_transcribe.theme")).toBe("dark");
  await actor.click(screen.getByRole("button", { name: /Account menu/ }));
  expect(
    await screen.findByRole("menuitem", { name: "My profile" }),
  ).toHaveAttribute("href", "/profile");
  expect(screen.getByRole("menuitem", { name: "Log out" })).toBeVisible();
  await actor.keyboard("{Escape}");
  await actor.click(
    screen.getByRole("button", { name: "Notifications (0 unread)" }),
  );
  expect(
    await screen.findByRole("dialog", { name: "Notifications" }),
  ).toBeVisible();
  await actor.click(screen.getByRole("switch", { name: "Unread only" }));
  expect(screen.getByRole("switch", { name: "Unread only" })).toBeChecked();
  await actor.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: "Theme" }));
  await actor.click(
    await screen.findByRole("menuitemradio", { name: "Light" }),
  );
  expect(document.documentElement).not.toHaveClass("dark");
});

it("marks an invalid workspace name and lets Escape dismiss the form", async () => {
  const actor = userEvent.setup();
  render(<App />);
  const trigger = (
    await screen.findAllByRole("button", { name: "New workspace" })
  )[0];
  await actor.click(trigger);
  await actor.click(screen.getByRole("button", { name: "Create" }));
  const name = screen.getByRole("textbox", { name: /name/i });
  expect(name).toHaveAttribute("aria-invalid", "true");
  expect(name).toHaveAccessibleDescription();
  await actor.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await waitFor(() => expect(trigger).toHaveFocus());
});
