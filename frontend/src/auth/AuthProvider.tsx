import axios from "axios";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import * as authApi from "@/api/auth";
import { setOnAuthFailure } from "@/api/client";
import type { User } from "@/api/types";
import { AuthContext, type AuthContextValue } from "@/auth/authContext";
import {
  initializeIdentity,
  loginIdentity,
  logoutIdentity,
  clearIdentity,
} from "@/auth/keycloak";
import { useQueryClient } from "@tanstack/react-query";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [sessionError, setSessionError] = useState<"unavailable" | "denied">();
  const [isLoading, setIsLoading] = useState(true);

  const queryClient = useQueryClient();
  const logout = useCallback(() => {
    setUser(null);
    queryClient.clear();
    void logoutIdentity().catch(() => {
      clearIdentity();
    });
  }, [queryClient]);

  const login = useCallback(async (path?: string) => {
    setSessionError(undefined);
    await loginIdentity(path);
  }, []);

  const refreshUser = useCallback(async () => {
    const me = await authApi.getCurrentUser();
    setUser(me);
  }, []);

  // Log out if the Axios layer reports an unrecoverable auth failure.
  useEffect(() => {
    setOnAuthFailure(() => {
      setUser(null);
      queryClient.clear();
    });
    return () => setOnAuthFailure(null);
  }, [queryClient]);

  // Restore the session on first load.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const identity = await initializeIdentity();
        if (!identity.authenticated) return;
        const me = await authApi.getCurrentUser();
        if (active) setUser(me);
      } catch (error) {
        if (active)
          setSessionError(
            axios.isAxiosError(error) &&
              [401, 403].includes(error.response?.status ?? 0)
              ? "denied"
              : "unavailable",
          );
        clearIdentity();
      } finally {
        if (active) setIsLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  const value: AuthContextValue = {
    user,
    sessionError,
    isLoading,
    isAuthenticated: user !== null,
    isAdmin: user?.role === "admin",
    isPrivileged: user?.role === "admin" || user?.role === "power_user",
    login,
    logout,
    refreshUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
