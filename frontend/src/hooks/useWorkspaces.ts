/** Recording workspace metadata and membership. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { WorkspaceRole, WorkspaceVisibility } from "@/api/types";
import * as workspacesApi from "@/api/workspaces";

export const workspacesKeys = {
  all: ["workspaces"] as const,
  mine: () => [...workspacesKeys.all, "mine"] as const,
  detail: (id: string) => [...workspacesKeys.all, "detail", id] as const,
  members: (id: string) => [...workspacesKeys.all, "members", id] as const,
};

/** Workspaces the caller may open (owned, member, or shared). */
export function useMyWorkspaces() {
  return useQuery({
    queryKey: workspacesKeys.mine(),
    queryFn: workspacesApi.listMyWorkspaces,
  });
}

export function useWorkspace(id: string) {
  return useQuery({
    queryKey: workspacesKeys.detail(id),
    queryFn: () => workspacesApi.getWorkspace(id),
    enabled: !!id,
  });
}

export function useCreateWorkspace() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: {
      name: string;
      description?: string | null;
      visibility?: WorkspaceVisibility;
    }) => workspacesApi.createWorkspace(payload),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspacesKeys.all }),
  });
}

export function useDeleteWorkspace() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => workspacesApi.deleteWorkspace(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspacesKeys.all }),
  });
}

// --- Members ---------------------------------------------------------------

export function useWorkspaceMembers(id: string) {
  return useQuery({
    queryKey: workspacesKeys.members(id),
    queryFn: () => workspacesApi.listWorkspaceMembers(id),
    enabled: !!id,
  });
}

export function useAddWorkspaceMember(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      userId,
      email,
      role,
    }: {
      userId?: string;
      email?: string;
      role: WorkspaceRole;
    }) => workspacesApi.addWorkspaceMember(id, { userId, email }, role),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspacesKeys.members(id) }),
  });
}

export function useRemoveWorkspaceMember(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) =>
      workspacesApi.removeWorkspaceMember(id, userId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspacesKeys.members(id) }),
  });
}

export function useUpdateWorkspaceMemberRole(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: WorkspaceRole }) =>
      workspacesApi.updateWorkspaceMemberRole(id, userId, role),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: workspacesKeys.members(id) }),
  });
}
