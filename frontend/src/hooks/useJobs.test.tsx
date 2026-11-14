import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as jobsApi from "@/api/jobs";
import type { Job } from "@/api/types";
import { useJobs } from "@/hooks/useJobs";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("keeps polling a retrying job until it finishes", async () => {
  const retrying = { task_id: "retrying", status: "retry" } as Job;
  const list = vi
    .spyOn(jobsApi, "listJobs")
    .mockResolvedValueOnce([retrying])
    .mockResolvedValue([{ ...retrying, status: "success" }]);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  // Install fake timers before React Query schedules its polling interval.
  vi.useFakeTimers();
  const { result, unmount } = renderHook(() => useJobs({}), { wrapper });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
  expect(result.current.data?.[0].status).toBe("retry");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4001);
  });
  expect(result.current.data?.[0].status).toBe("success");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(8000);
  });
  expect(list).toHaveBeenCalledTimes(2);
  unmount();
  client.clear();
});
