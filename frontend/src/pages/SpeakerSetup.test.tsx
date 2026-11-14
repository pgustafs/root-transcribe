import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { ToastProvider } from "@/components/toast/ToastProvider";
import { server } from "@/test/msw/server";
import { SpeakersPage } from "./SpeakersPage";

vi.mock("@/auth/useAuth", () => ({
  useAuth: () => ({ isAdmin: true, isPrivileged: true }),
}));
let submitted: unknown;
beforeEach(() => {
  submitted = undefined;
  server.use(
    http.get("*/api/v1/config", () =>
      HttpResponse.json({
        project_name: "Root Transcribe",
        meetings_enabled: true,
      }),
    ),
    http.get("*/api/v1/speakers", () => HttpResponse.json([])),
    http.get("*/api/v1/speakers/compliance-exports", () =>
      HttpResponse.json([]),
    ),
    http.post("*/api/v1/speakers", async ({ request }) => {
      submitted = await request.json();
      return HttpResponse.json({ id: "new-speaker" }, { status: 201 });
    }),
  );
});
function showPage() {
  render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <MemoryRouter
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          <ToastProvider>
            <SpeakersPage />
          </ToastProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

it("adds a name without enrolling a voice or claiming consent", async () => {
  const actor = userEvent.setup();
  showPage();
  await actor.click(
    (await screen.findAllByRole("button", { name: "Add speaker" }))[0],
  );
  await actor.type(screen.getByRole("textbox", { name: /name/i }), "Alex");
  await actor.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(submitted).toEqual({ display_name: "Alex", consent_granted: false }),
  );
});

it("requires and submits evidence when an admin records another person's voice consent", async () => {
  const actor = userEvent.setup();
  showPage();
  await actor.click(
    (await screen.findAllByRole("button", { name: "Add speaker" }))[0],
  );
  await actor.type(screen.getByRole("textbox", { name: /name/i }), "Alex");
  await actor.click(
    screen.getByRole("button", { name: "Optional voice recognition" }),
  );
  await actor.click(
    screen.getByRole("checkbox", { name: /consented to voice enrollment/i }),
  );
  await actor.click(screen.getByRole("button", { name: "Save" }));
  expect(submitted).toBeUndefined();
  expect(screen.getByText(/Describe how consent was obtained/)).toBeVisible();
  await actor.type(
    screen.getByRole("textbox", { name: /How was consent obtained/i }),
    "Written agreement on 3 November 2026",
  );
  await actor.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(submitted).toEqual({
      display_name: "Alex",
      consent_granted: true,
      consent_evidence: "Written agreement on 3 November 2026",
    }),
  );
});
