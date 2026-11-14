/** React Query hooks for the global notification bell.
 * The unread badge polls every 30 s — no push channel exists; SSE can replace
 * the poll later without schema changes. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as api from "@/api/notifications";

export const notificationsKeys = {
  all: ["notifications"] as const,
  list: () => [...notificationsKeys.all, "list"] as const,
  unread: () => [...notificationsKeys.all, "unread"] as const,
};

export function useUnreadNotificationCount(enabled = true) {
  return useQuery({
    queryKey: notificationsKeys.unread(),
    queryFn: api.getUnreadCount,
    enabled,
    refetchInterval: 30_000,
  });
}

export function useNotifications(enabled = true) {
  return useQuery({
    queryKey: notificationsKeys.list(),
    queryFn: () => api.listNotifications(),
    enabled,
  });
}

export function useMarkNotificationRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, read }: { id: string; read: boolean }) =>
      api.markNotificationRead(id, read),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: notificationsKeys.all }),
  });
}

export function useMarkAllNotificationsRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: api.markAllNotificationsRead,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: notificationsKeys.all }),
  });
}
