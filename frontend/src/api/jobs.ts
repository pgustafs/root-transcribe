/** Background jobs (Celery) API: admin-only monitor + cancel. */

import { apiClient } from "@/api/client";
import type { Job, JobListParams, JobSummary } from "@/api/types";

const BASE = "/jobs";

export async function listJobs(params: JobListParams = {}): Promise<Job[]> {
  // Axios omits params whose value is undefined.
  const { data } = await apiClient.get<Job[]>(BASE, { params });
  return data;
}

export async function getJobsSummary(): Promise<JobSummary> {
  const { data } = await apiClient.get<JobSummary>(`${BASE}/summary`);
  return data;
}

/** Cancel an in-flight job: revokes/terminates the task and marks it revoked. */
export async function cancelJob(taskId: string): Promise<Job> {
  const { data } = await apiClient.post<Job>(`${BASE}/${taskId}/cancel`);
  return data;
}
