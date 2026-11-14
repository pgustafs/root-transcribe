/** React Query hooks for the users resource. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as usersApi from "@/api/users";
import type { UserAccessUpdate } from "@/api/types";

export const usersKeys = {
  all: ["users"] as const,
  list: () => [...usersKeys.all, "list"] as const,
};

export function useUsers(enabled = true) {
  return useQuery({
    queryKey: usersKeys.list(),
    queryFn: () => usersApi.listUsers({ limit: 200 }),
    enabled,
  });
}

export function useUpdateUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: UserAccessUpdate }) =>
      usersApi.updateUser(id, payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: usersKeys.all }),
  });
}
