import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider } from "react-i18next";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import i18n from "@/i18n";
import { server } from "@/test/msw/server";
import { RecordMeetingModal } from "@/components/workspace/RecordMeetingModal";

const { start } = vi.hoisted(() => ({ start: vi.fn(async () => true) }));
vi.mock("@/components/recording/RecordingProvider", () => ({
  useRecordingContext: () => ({
    start,
    isSupported: true,
    error: null,
    startError: null,
  }),
}));
vi.mock("@/hooks/useMediaRecorder", () => ({
  listAudioInputs: async () => [{ deviceId: "m1", label: "Desk microphone" }],
}));

beforeEach(() => {
  start.mockClear();
  server.use(
    http.get("*/api/v1/config", () =>
      HttpResponse.json({
        meetings_status: "available",
        meeting_consent_required: false,
      }),
    ),
  );
});

async function openDialog() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <RecordMeetingModal workspaceId="ws" isOpen onClose={() => {}} />
      </QueryClientProvider>
    </I18nextProvider>,
  );
  return within(
    await screen.findByRole("group", { name: "What are you recording?" }),
  );
}

const startRecording = (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole("button", { name: "Start recording" }));

it("records an online meeting by default, with just me at the microphone", async () => {
  const user = userEvent.setup();
  const source = await openDialog();
  expect(
    source.getByRole("button", { name: "An online meeting" }),
  ).toHaveAttribute("aria-pressed", "true");
  // The two sides are counted apart.
  expect(screen.getByLabelText("People at your microphone")).toHaveValue("1");
  expect(screen.getByLabelText("People in the call")).toHaveValue("");
  await user.selectOptions(screen.getByLabelText("People in the call"), "3");
  await startRecording(user);
  await waitFor(() =>
    expect(start).toHaveBeenCalledWith(
      expect.objectContaining({
        source: "mic_tab",
        micSpeakers: 1,
        expectedSpeakers: 3,
      }),
    ),
  );
});

it("counts a meeting room that joins a call together", async () => {
  const user = userEvent.setup();
  await openDialog();
  await user.selectOptions(
    screen.getByLabelText("People at your microphone"),
    "3",
  );
  await startRecording(user);
  await waitFor(() =>
    expect(start).toHaveBeenCalledWith(
      expect.objectContaining({ source: "mic_tab", micSpeakers: 3 }),
    ),
  );
});

it("records a tab alone without a microphone", async () => {
  const user = userEvent.setup();
  const source = await openDialog();
  // The microphone can be chosen while it is part of the recording ...
  expect(await screen.findByLabelText("Microphone")).toBeInTheDocument();
  await user.click(source.getByRole("button", { name: "A tab only" }));
  // ... and is not offered when it is not.
  expect(screen.queryByLabelText("Microphone")).toBeNull();
  expect(screen.queryByLabelText("People at your microphone")).toBeNull();
  await user.selectOptions(screen.getByLabelText("Number of speakers"), "2");
  await startRecording(user);
  await waitFor(() =>
    expect(start).toHaveBeenCalledWith(
      expect.objectContaining({
        source: "tab",
        micSpeakers: null,
        expectedSpeakers: 2,
      }),
    ),
  );
});

it("records the people in this room with the microphone only", async () => {
  const user = userEvent.setup();
  const source = await openDialog();
  await user.click(source.getByRole("button", { name: "People in this room" }));
  expect(screen.queryByLabelText("People in the call")).toBeNull();
  expect(screen.getByLabelText("Number of speakers")).toHaveValue("");
  await startRecording(user);
  await waitFor(() =>
    expect(start).toHaveBeenCalledWith(
      expect.objectContaining({ source: "mic", micSpeakers: null }),
    ),
  );
});
