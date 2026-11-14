/** Application profile lookup after Keycloak authentication. */
import { apiClient } from "@/api/client";
import type { User } from "@/api/types";

export async function getCurrentUser(): Promise<User> {
  const { data } = await apiClient.get<User>("/users/me");
  return data;
}
