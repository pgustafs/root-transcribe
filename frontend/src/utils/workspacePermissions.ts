/** Frontend mirror of the backend's four-role workspace policy
 * (`app/services/workspace_access.py`). Drives which actions render at all —
 * the backend enforces the same rules on every request. */

import type { WorkspaceRole } from "@/api/types";

export type WorkspacePermissions = {
  /** Recording content (editor and up). */
  canEditContent: boolean;
  /** Add/remove members and change non-owner roles (manager and up). */
  canManageMembers: boolean;
  /** Grant or revoke the owner member role (owner only). */
  canManageOwners: boolean;
  isOwner: boolean;
};

export function permissionsFor(
  role: WorkspaceRole | null | undefined,
): WorkspacePermissions {
  const isOwner = role === "owner";
  const canManage = isOwner || role === "manager";
  const canEditContent = canManage || role === "editor";
  return {
    canEditContent,
    canManageMembers: canManage,
    canManageOwners: isOwner,
    isOwner,
  };
}
