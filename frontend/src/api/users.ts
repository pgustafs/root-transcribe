/** User management API calls (mirrors backend /users routes). */

import { apiClient } from "@/api/client";
import type { User, UserAccessUpdate, UserProfileUpdate } from "@/api/types";

export async function listUsers(params?: {
  skip?: number;
  limit?: number;
}): Promise<User[]> {
  const { data } = await apiClient.get<User[]>("/users", { params });
  return data;
}

export async function updateUser(
  id: string,
  payload: UserAccessUpdate,
): Promise<User> {
  const { data } = await apiClient.patch<User>(`/users/${id}`, payload);
  return data;
}

/** Update the authenticated user's own profile. */
export async function updateCurrentUser(
  payload: UserProfileUpdate,
): Promise<User> {
  const { data } = await apiClient.patch<User>("/users/me", payload);
  return data;
}
