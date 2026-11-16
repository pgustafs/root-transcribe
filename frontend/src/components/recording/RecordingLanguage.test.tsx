import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider } from "react-i18next";
import { http, HttpResponse } from "msw";
import { expect, it, vi } from "vitest";
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
  listAudioInputs: async () => [],
}));

it.each(["Auto", "English", "Swedish"])(
  "starts a recording with %s independently of interface language",
  async (label) => {
    start.mockClear();
    server.use(
      http.get("*/api/v1/config", () =>
        HttpResponse.json({
          meetings_status: "available",
          meeting_consent_required: false,
        }),
      ),
    );
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
    const user = userEvent.setup();
    const group = await screen.findByRole("group", {
      name: "Recording language",
    });
    const option = within(group).getByRole("button", { name: label });
    await user.click(option);
    // Clicking the selected choice again must not clear the required selection.
    await user.click(option);
    expect(option).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Start recording" }));
    await waitFor(() =>
      expect(start).toHaveBeenCalledWith(
        expect.objectContaining({
          requestedLanguage: { Auto: "auto", English: "en", Swedish: "sv" }[
            label
          ],
        }),
      ),
    );
  },
);
