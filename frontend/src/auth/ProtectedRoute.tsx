/** Route guard: requires an authenticated session. */

import { type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { FullPageSpinner } from "@/components/FullPageSpinner";
import { useAuth } from "@/auth/useAuth";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return <FullPageSpinner />;
  }
  if (!isAuthenticated) {
    // Preserve where the user was headed so login can return them there.
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}
