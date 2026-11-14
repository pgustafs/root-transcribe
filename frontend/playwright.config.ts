import { defineConfig, devices } from "@playwright/test";

/** Opt-in smoke tests against a running standalone deployment. */
const mockApi = !!process.env.E2E_MOCK_API;
const previewURL = "http://127.0.0.1:4173";
const baseURL =
  process.env.E2E_BASE_URL ?? (mockApi ? previewURL : "http://localhost:8095");

export default defineConfig({
  testDir: "./e2e",
  // Run serially: the tests share one stateful backend, and a single API worker
  // can be overwhelmed by parallel browsers (transient 502s through nginx).
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: mockApi
    ? {
        command: "npx vite preview --host 127.0.0.1 --port 4173 --strictPort",
        url: previewURL,
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      }
    : undefined,
});
