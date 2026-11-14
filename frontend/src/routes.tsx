/** Application routes and access guards. */
import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AdminRoute } from "@/auth/AdminRoute";
import { PrivilegedRoute } from "@/auth/PrivilegedRoute";
import { ProtectedRoute } from "@/auth/ProtectedRoute";
import { AppLayout } from "@/components/AppLayout";
import { FullPageSpinner } from "@/components/FullPageSpinner";
import { LoginPage } from "@/pages/LoginPage";
import { ForbiddenPage } from "@/pages/ForbiddenPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
const WorkspacesPage = lazy(() =>
  import("@/pages/WorkspacesPage").then((m) => ({ default: m.WorkspacesPage })),
);
const WorkspaceMeetingsPage = lazy(() =>
  import("@/pages/WorkspaceMeetingsPage").then((m) => ({
    default: m.WorkspaceMeetingsPage,
  })),
);
const SpeakersPage = lazy(() =>
  import("@/pages/SpeakersPage").then((m) => ({ default: m.SpeakersPage })),
);
const UsersPage = lazy(() =>
  import("@/pages/UsersPage").then((m) => ({ default: m.UsersPage })),
);
const JobsPage = lazy(() =>
  import("@/pages/JobsPage").then((m) => ({ default: m.JobsPage })),
);
const ProfilePage = lazy(() =>
  import("@/pages/ProfilePage").then((m) => ({ default: m.ProfilePage })),
);
export function AppRoutes() {
  return (
    <Suspense fallback={<FullPageSpinner />}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          element={
            <ProtectedRoute>
              <AppLayout />
            </ProtectedRoute>
          }
        >
          <Route index element={<Navigate to="/workspaces" replace />} />
          <Route path="/workspaces" element={<WorkspacesPage />} />
          <Route path="/workspaces/:id" element={<WorkspaceMeetingsPage />} />
          <Route
            path="/workspaces/:id/meetings"
            element={<WorkspaceMeetingsPage />}
          />
          <Route
            path="/admin/speakers"
            element={
              <PrivilegedRoute>
                <SpeakersPage />
              </PrivilegedRoute>
            }
          />
          <Route
            path="/users"
            element={
              <AdminRoute>
                <UsersPage />
              </AdminRoute>
            }
          />
          <Route
            path="/admin/jobs"
            element={
              <AdminRoute>
                <JobsPage />
              </AdminRoute>
            }
          />
          <Route path="/profile" element={<ProfilePage />} />
          <Route path="/forbidden" element={<ForbiddenPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
