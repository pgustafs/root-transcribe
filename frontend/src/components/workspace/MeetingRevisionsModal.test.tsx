import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it, vi } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { server } from "@/test/msw/server";
import { ToastProvider } from "@/components/toast/ToastProvider";
import { MeetingRevisionsModal } from "./MeetingRevisionsModal";

it("compares the selected revision and approves that revision with the review note", async () => {
  const actor = userEvent.setup();
  const onClose = vi.fn();
  const base = "*/api/v1/workspaces/w1/recordings/r1/revisions";
  let approved: unknown;
  server.use(
    http.get(base, () =>
      HttpResponse.json([
        {
          id: "v2",
          revision_number: 2,
          state: "machine_candidate",
          conflict_count: 0,
          carried_edits: 0,
        },
        {
          id: "v1",
          revision_number: 1,
          state: "approved",
          conflict_count: 0,
          carried_edits: 0,
        },
      ]),
    ),
    http.get(`${base}/:id/segments`, ({ params }) =>
      HttpResponse.json([
        {
          speaker_label: "SPEAKER_00",
          speaker_name: "Alex",
          text: params.id === "v1" ? "Original words" : "Corrected words",
        },
      ]),
    ),
    http.post(`${base}/:id/approve`, async ({ request, params }) => {
      approved = { id: params.id, ...((await request.json()) as object) };
      return HttpResponse.json({ id: params.id, state: "approved" });
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <ToastProvider>
          <MeetingRevisionsModal
            workspaceId="w1"
            recordingId="r1"
            displayRevisionId="v1"
            canEdit
            onClose={onClose}
          />
        </ToastProvider>
      </QueryClientProvider>
    </I18nextProvider>,
  );
  expect(
    await screen.findByRole("dialog", { name: "Transcript revisions" }),
  ).toBeVisible();
  const candidate = await screen.findByRole("button", { name: /Revision 2/ });
  expect(candidate).toHaveAttribute("aria-pressed", "true");
  await actor.click(screen.getByRole("button", { name: /Revision 1/ }));
  expect(await screen.findByText("Alex: Original words")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Approve revision" }),
  ).not.toBeInTheDocument();
  await actor.click(candidate);
  await actor.type(screen.getByRole("textbox"), "Reviewed against audio");
  await actor.click(screen.getByRole("button", { name: "Approve revision" }));
  await waitFor(() =>
    expect(approved).toEqual({ id: "v2", note: "Reviewed against audio" }),
  );
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
});
