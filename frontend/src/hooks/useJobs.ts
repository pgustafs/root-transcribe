/** React Query hooks for the background-jobs (Celery) monitor. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as jobsApi from "@/api/jobs";
import type { Job, JobListParams } from "@/api/types";

export const jobKeys = {
  all: ["jobs"] as const,
  list: (params: JobListParams) => [...jobKeys.all, "list", params] as const,
  summary: () => [...jobKeys.all, "summary"] as const,
};

// Poll while work is pending, running or waiting for a retry.
const POLL_MS = 4000;

function hasActive(jobs: Job[] | undefined): boolean {
  return Boolean(
    jobs?.some((j) => ["pending", "started", "retry"].includes(j.status)),
  );
}

/** List jobs (filtered); auto-refreshes while any job can still progress. */
export function useJobs(params: JobListParams) {
  return useQuery({
    queryKey: jobKeys.list(params),
    queryFn: () => jobsApi.listJobs(params),
    refetchInterval: (query) => (hasActive(query.state.data) ? POLL_MS : false),
  });
}

/** Header summary counts; auto-refreshes while work is active. */
export function useJobsSummary() {
  return useQuery({
    queryKey: jobKeys.summary(),
    queryFn: jobsApi.getJobsSummary,
    refetchInterval: (query) => (query.state.data?.active ? POLL_MS : false),
  });
}

/** Cancel an in-flight job; refreshes the list + summary on success. */
export function useCancelJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => jobsApi.cancelJob(taskId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: jobKeys.all }),
  });
}
