/**
 * Factory for role-gated route guards. Every role guard shares the same shape —
 * wait for session restore, bounce anonymous users to /login, and send
 * authenticated-but-unauthorized users to /forbidden — differing only in which
 * role flag they check. `createRoleRoute` captures that predicate so each guard
 * is a one-liner instead of a copy of the same component.
 */

import { type ReactNode } from "react";
import { Navigate } from "react-router-dom";

import { type AuthContextValue } from "@/auth/authContext";
import { useAuth } from "@/auth/useAuth";
import { FullPageSpinner } from "@/components/FullPageSpinner";

export function createRoleRoute(hasRole: (auth: AuthContextValue) => boolean) {
  return function RoleRoute({ children }: { children: ReactNode }) {
    const auth = useAuth();

    if (auth.isLoading) {
      return <FullPageSpinner />;
    }
    if (!auth.isAuthenticated) {
      return <Navigate to="/login" replace />;
    }
    if (!hasRole(auth)) {
      return <Navigate to="/forbidden" replace />;
    }
    return <>{children}</>;
  };
}
