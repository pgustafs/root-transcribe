/** Recording workspace metadata and membership. */

import { apiClient } from "@/api/client";
import type {
  Workspace,
  WorkspaceMember,
  WorkspaceRole,
  WorkspaceVisibility,
} from "@/api/types";

/** Workspaces the caller may open (owned, a member of, or org-wide shared). */
export async function listMyWorkspaces(): Promise<Workspace[]> {
  const { data } = await apiClient.get<Workspace[]>("/workspaces/mine");
  return data;
}

export async function getWorkspace(id: string): Promise<Workspace> {
  const { data } = await apiClient.get<Workspace>(`/workspaces/${id}`);
  return data;
}

export async function createWorkspace(payload: {
  name: string;
  description?: string | null;
  visibility?: WorkspaceVisibility;
}): Promise<Workspace> {
  const { data } = await apiClient.post<Workspace>("/workspaces", payload);
  return data;
}

export async function deleteWorkspace(id: string): Promise<void> {
  await apiClient.delete(`/workspaces/${id}`);
}

// --- Members ---------------------------------------------------------------

export async function listWorkspaceMembers(
  id: string,
): Promise<WorkspaceMember[]> {
  const { data } = await apiClient.get<WorkspaceMember[]>(
    `/workspaces/${id}/members`,
  );
  return data;
}

/** Identify the member to add by id (admin picker) or exact email (owners). */
export type WorkspaceMemberTarget = { userId?: string; email?: string };

export async function addWorkspaceMember(
  id: string,
  target: WorkspaceMemberTarget,
  role: WorkspaceRole,
): Promise<WorkspaceMember> {
  const { data } = await apiClient.post<WorkspaceMember>(
    `/workspaces/${id}/members`,
    { user_id: target.userId, email: target.email, role },
  );
  return data;
}

export async function removeWorkspaceMember(
  id: string,
  userId: string,
): Promise<void> {
  await apiClient.delete(`/workspaces/${id}/members/${userId}`);
}

export async function updateWorkspaceMemberRole(
  id: string,
  userId: string,
  role: WorkspaceRole,
): Promise<WorkspaceMember> {
  const { data } = await apiClient.patch<WorkspaceMember>(
    `/workspaces/${id}/members/${userId}`,
    { role },
  );
  return data;
}
