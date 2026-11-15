import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { I18nextProvider } from "react-i18next";
import { beforeEach, expect, it } from "vitest";

import type { DlpFinding, DlpTerm } from "@/api/types";
import i18n from "@/i18n";
import { server } from "@/test/msw/server";

import { MeetingDlpPanel } from "./MeetingDlpPanel";

const TERMS = "*/api/v1/workspaces/w1/meetings/dlp-terms";
const DLP = "*/api/v1/workspaces/w1/recordings/r1/dlp";

let terms: DlpTerm[];
let findings: DlpFinding[];
let scans: number;

const finding = (text: string): DlpFinding => ({
  id: `f-${text}`,
  recording_id: "r1",
  revision_id: "v1",
  segment_id: "s1",
  detector: "term",
  term_label: text,
  matched_text: text,
  start_char: 0,
  end_char: text.length,
  start_ms: 61000,
  end_ms: 63000,
  status: "pending",
  created_at: "2026-11-15T10:00:00Z",
});

beforeEach(() => {
  terms = [];
  findings = [];
  scans = 0;
  server.use(
    http.get(TERMS, () => HttpResponse.json(terms)),
    http.post(TERMS, async ({ request }) => {
      const body = (await request.json()) as {
        pattern: string;
        is_regex: boolean;
        label: string | null;
      };
      const term: DlpTerm = {
        id: `t${terms.length + 1}`,
        active: true,
        created_by_email: "demo@example.com",
        created_at: "2026-11-15T10:00:00Z",
        ...body,
      };
      terms = [...terms, term];
      return HttpResponse.json(term);
    }),
    http.delete(`${TERMS}/:id`, ({ params }) => {
      terms = terms.filter((term) => term.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(DLP, () => HttpResponse.json(findings)),
    http.post(`${DLP}/scan`, () => {
      scans += 1;
      // The worker's result: one finding per workspace word.
      findings = terms.map((term) => finding(term.pattern));
      return HttpResponse.json({ status: "scanning" }, { status: 202 });
    }),
  );
});

function showPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <MeetingDlpPanel workspaceId="w1" recordingId="r1" onSeek={() => {}} />
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

it("says what the scan looks for when nothing was found", async () => {
  showPanel();
  expect(await screen.findByText("Nothing found")).toBeInTheDocument();
  for (const name of [
    "Email addresses",
    "Phone numbers",
    "Payment card numbers",
    "Passwords and keys",
  ])
    expect(screen.getByText(name)).toBeInTheDocument();
});

it("adds a word, scans and lists what the scan found", async () => {
  const user = userEvent.setup();
  showPanel();
  await user.type(
    await screen.findByLabelText("Word or phrase to look for"),
    "Project Falcon",
  );
  await user.click(screen.getByRole("button", { name: "Add" }));

  expect(
    await screen.findByRole("button", {
      name: "Stop looking for Project Falcon",
    }),
  ).toBeInTheDocument();
  await waitFor(() => expect(scans).toBe(1));
  // The list is re-read while the scan is watched: the finding appears.
  expect(
    await screen.findByRole("button", { name: "Accept" }, { timeout: 4000 }),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Scanning…" })).toBeDisabled();
});

it("removes a word and scans again", async () => {
  terms = [
    {
      id: "t1",
      pattern: "Falcon",
      is_regex: false,
      label: "Falcon",
      active: true,
      created_by_email: null,
      created_at: "2026-11-15T10:00:00Z",
    },
  ];
  const user = userEvent.setup();
  showPanel();
  await user.click(
    await screen.findByRole("button", { name: "Stop looking for Falcon" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "Stop looking for Falcon" }),
    ).not.toBeInTheDocument(),
  );
  await waitFor(() => expect(scans).toBe(1));
});
