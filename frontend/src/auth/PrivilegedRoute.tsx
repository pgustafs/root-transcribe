/** Route guard: requires an elevated user (admin or power user). */

import { createRoleRoute } from "@/auth/createRoleRoute";

export const PrivilegedRoute = createRoleRoute((auth) => auth.isPrivileged);
