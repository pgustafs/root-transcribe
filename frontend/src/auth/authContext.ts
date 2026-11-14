/** Auth context definition (kept separate from the provider component). */

import { createContext } from "react";

import type { User } from "@/api/types";

export type AuthContextValue = {
  /** The authenticated user, or null when logged out. */
  user: User | null;
  /** True while the initial session is being restored on app load. */
  isLoading: boolean;
  sessionError?: "unavailable" | "denied";
  isAuthenticated: boolean;
  isAdmin: boolean;
  /** True for elevated users (admin or power user). */
  isPrivileged: boolean;
  login: (path?: string) => Promise<void>;
  logout: () => void;
  /** Re-fetch the current user (e.g. after a profile update). */
  refreshUser: () => Promise<void>;
};

export const AuthContext = createContext<AuthContextValue | undefined>(
  undefined,
);
