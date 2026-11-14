/** Route guard: requires the authenticated user to have the admin role. */

import { createRoleRoute } from "@/auth/createRoleRoute";

export const AdminRoute = createRoleRoute((auth) => auth.isAdmin);
