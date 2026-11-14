/** Global in-app notifications (the masthead bell). */

import { apiClient } from "@/api/client";
import type { AppNotification } from "@/api/types";

export async function listNotifications(
  unreadOnly = false,
): Promise<AppNotification[]> {
  const { data } = await apiClient.get<AppNotification[]>("/notifications", {
    params: { unread_only: unreadOnly },
  });
  return data;
}

export async function getUnreadCount(): Promise<number> {
  const { data } = await apiClient.get<{ count: number }>(
    "/notifications/unread-count",
  );
  return data.count;
}

export async function markNotificationRead(
  id: string,
  read: boolean,
): Promise<AppNotification> {
  const { data } = await apiClient.post<AppNotification>(
    `/notifications/${id}/${read ? "read" : "unread"}`,
  );
  return data;
}

export async function markAllNotificationsRead(): Promise<void> {
  await apiClient.post("/notifications/read-all");
}
