import { UsersIcon } from "lucide-react";
import {
  NativeSelectOption,
  NativeSelect,
} from "@/components/ui/native-select";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  TableHead,
  TableRow,
  TableHeader,
  TableCell,
  TableBody,
  Table,
} from "@/components/ui/table";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import type { WorkspaceRole } from "@/api/types";
import { useAuth } from "@/auth/useAuth";
import { Alert } from "@/components/Alert";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { DataState } from "@/components/DataState";
import { useToast } from "@/components/toast/useToast";
import { useUsers } from "@/hooks/useUsers";
import {
  useAddWorkspaceMember,
  useRemoveWorkspaceMember,
  useUpdateWorkspaceMemberRole,
  useWorkspaceMembers,
} from "@/hooks/useWorkspaces";
import { WORKSPACE_ROLE_LABELS } from "@/lib/labels";
import type { WorkspacePermissions } from "@/utils/workspacePermissions";

const ROLES: WorkspaceRole[] = ["owner", "manager", "editor", "viewer"];

export function WorkspaceMembersTab({
  workspaceId,
  permissions,
}: {
  workspaceId: string;
  permissions: WorkspacePermissions;
}) {
  const { t } = useTranslation(["access", "common"]);
  const { isAdmin } = useAuth();
  const toast = useToast();
  const canEdit = permissions.canManageMembers;
  // Only owners may grant/revoke the owner member role.
  const roleOptions = permissions.canManageOwners
    ? ROLES
    : ROLES.filter((r) => r !== "owner");
  const membersQuery = useWorkspaceMembers(workspaceId);
  // The full user directory is admin-only; owners/managers add by exact email.
  const { data: users = [] } = useUsers(isAdmin);
  const addMember = useAddWorkspaceMember(workspaceId);
  const setRole = useUpdateWorkspaceMemberRole(workspaceId);
  const removeMember = useRemoveWorkspaceMember(workspaceId);

  const [selectedUser, setSelectedUser] = useState("");
  const [email, setEmail] = useState("");
  const [newRole, setNewRole] = useState<WorkspaceRole>("viewer");
  const [removing, setRemoving] = useState<{
    userId: string;
    member: string;
  } | null>(null);

  const memberIds = useMemo(
    () => new Set((membersQuery.data ?? []).map((m) => m.user_id)),
    [membersQuery.data],
  );
  const candidates = users.filter((u) => !memberIds.has(u.id));

  const submitAdd = () => {
    addMember.mutate(
      isAdmin
        ? { userId: selectedUser, role: newRole }
        : { email: email.trim(), role: newRole },
      {
        onSuccess: (member) => {
          setSelectedUser("");
          setEmail("");
          toast.success(
            t("access:workspaces.detail.memberAdded", {
              member: member.email ?? member.user_id,
            }),
          );
        },
      },
    );
  };

  const changeRole = (userId: string, role: WorkspaceRole) =>
    setRole.mutate(
      { userId, role },
      {
        onError: (error) =>
          toast.error(
            getErrorMessage(error, t("access:workspaces.detail.roleError")),
          ),
      },
    );

  const confirmRemove = () => {
    if (!removing) return;
    const { userId, member } = removing;
    removeMember.mutate(userId, {
      onSuccess: () => {
        setRemoving(null);
        toast.success(t("access:workspaces.detail.memberRemoved", { member }));
      },
      onError: (error) => {
        setRemoving(null);
        toast.error(
          getErrorMessage(error, t("access:workspaces.detail.removeError")),
        );
      },
    });
  };

  return (
    <div className="flex flex-col gap-4">
      {canEdit && (
        <div>
          <div className="flex flex-col gap-4">
            {addMember.isError && (
              <div>
                <Alert
                  variant="danger"
                  isInline={true}
                  isLiveRegion={true}
                  title={getErrorMessage(
                    addMember.error,
                    t("access:workspaces.detail.addMemberError"),
                  )}
                />
              </div>
            )}
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <div>
                  {isAdmin ? (
                    <NativeSelect
                      value={selectedUser}
                      aria-label={t("access:workspaces.detail.selectUser")}
                      onChange={(_e) => {
                        const v = _e.currentTarget.value;
                        return setSelectedUser(v);
                      }}
                    >
                      <NativeSelectOption value="">
                        {t("access:workspaces.detail.selectUser")}
                      </NativeSelectOption>
                      {candidates.map((u) => (
                        <NativeSelectOption key={u.id} value={u.id}>
                          {u.email}
                        </NativeSelectOption>
                      ))}
                    </NativeSelect>
                  ) : (
                    /* No user directory for non-admins - add by exact email. */
                    <Input
                      type="email"
                      value={email}
                      aria-label={t("access:workspaces.detail.emailLabel")}
                      placeholder={t(
                        "access:workspaces.detail.emailPlaceholder",
                      )}
                      onChange={(_e) => {
                        const v = _e.currentTarget.value;
                        return setEmail(v);
                      }}
                    />
                  )}
                </div>
                <div>
                  <NativeSelect
                    value={newRole}
                    aria-label={t("access:workspaces.detail.roleLabel")}
                    onChange={(_e) => {
                      const v = _e.currentTarget.value;
                      return setNewRole(v as WorkspaceRole);
                    }}
                  >
                    {roleOptions.map((r) => (
                      <NativeSelectOption key={r} value={r}>
                        {t(WORKSPACE_ROLE_LABELS[r])}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </div>
                <div>
                  <Button
                    onClick={submitAdd}
                    disabled={
                      (isAdmin ? !selectedUser : !email.trim()) ||
                      addMember.isPending
                    }
                    variant="outline"
                  >
                    {t("access:workspaces.detail.addMember")}
                  </Button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
      <div>
        <DataState
          isLoading={membersQuery.isLoading}
          error={membersQuery.error}
          onRetry={() => void membersQuery.refetch()}
          isEmpty={(membersQuery.data ?? []).length === 0}
          emptyTitle={t("access:workspaces.detail.noMembers")}
          emptyBody={t("access:workspaces.detail.noMembersBody")}
          emptyIcon={UsersIcon}
          headingLevel="h2"
        >
          <Table aria-label={t("access:workspaces.detail.membersAria")}>
            <TableHeader>
              <TableRow>
                <TableHead>
                  {t("access:workspaces.detail.memberEmail")}
                </TableHead>
                <TableHead>{t("access:workspaces.detail.roleLabel")}</TableHead>
                <TableHead>
                  <span className="sr-only">
                    {t("access:workspaces.colActions")}
                  </span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(membersQuery.data ?? []).map((m) => (
                <TableRow key={m.id}>
                  <TableCell>
                    {/* Emails are masked server-side for viewers. */}
                    {m.email ?? t("access:workspaces.detail.memberHidden")}
                  </TableCell>
                  <TableCell>
                    {canEdit &&
                    (m.role !== "owner" || permissions.canManageOwners) ? (
                      <NativeSelect
                        value={m.role}
                        aria-label={t("access:workspaces.detail.roleFor", {
                          member: m.email ?? m.user_id,
                        })}
                        onChange={(_e) => {
                          const v = _e.currentTarget.value;
                          return changeRole(m.user_id, v as WorkspaceRole);
                        }}
                      >
                        {roleOptions.map((r) => (
                          <NativeSelectOption key={r} value={r}>
                            {t(WORKSPACE_ROLE_LABELS[r])}
                          </NativeSelectOption>
                        ))}
                      </NativeSelect>
                    ) : (
                      t(WORKSPACE_ROLE_LABELS[m.role])
                    )}
                  </TableCell>
                  <TableCell>
                    {canEdit &&
                      (m.role !== "owner" || permissions.canManageOwners) && (
                        <Button
                          aria-label={t(
                            "access:workspaces.detail.removeMember",
                            { member: m.email ?? m.user_id },
                          )}
                          onClick={() =>
                            setRemoving({
                              userId: m.user_id,
                              member: m.email ?? m.user_id,
                            })
                          }
                          disabled={removeMember.isPending}
                          variant="destructive"
                        >
                          {t("common:actions.remove")}
                        </Button>
                      )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </DataState>
      </div>
      {removing && (
        <ConfirmDeleteModal
          isOpen={true}
          title={t("access:workspaces.detail.removeMemberTitle")}
          itemName={removing.member}
          consequence={t("access:workspaces.detail.removeMemberConsequence")}
          confirmLabel={t("access:workspaces.detail.removeMemberConfirm")}
          isDeleting={removeMember.isPending}
          onConfirm={confirmRemove}
          onClose={() => setRemoving(null)}
        />
      )}
    </div>
  );
}
