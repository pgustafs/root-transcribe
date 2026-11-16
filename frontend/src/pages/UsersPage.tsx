import {
  ExternalLinkIcon as ExternalLinkAltIcon,
  UsersIcon,
  XIcon,
} from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  TableHead,
  TableRow,
  TableHeader,
  TableCell,
  TableBody,
  Table,
} from "@/components/ui/table";
import { CopyValue } from "@/components/CopyValue";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import type { Role, User } from "@/api/types";
import { useAuth } from "@/auth/useAuth";
import { FilterSelect } from "@/components/FilterSelect";
import { Alert } from "@/components/Alert";
import { DataState } from "@/components/DataState";
import { PageHeader } from "@/components/PageHeader";
import { Pagination } from "@/components/Pagination";
import { SearchInput } from "@/components/SearchInput";
import { StatusLabel } from "@/components/StatusLabel";
import { useToast } from "@/components/toast/useToast";
import { useConfig } from "@/hooks/useConfig";
import { useUpdateUser, useUsers } from "@/hooks/useUsers";
import { ACTIVE_STATUS, ROLE_LABELS } from "@/lib/labels";

export function UsersPage() {
  const { t } = useTranslation(["users", "common"]);
  const { user: currentUser } = useAuth();
  const toast = useToast();
  const usersQuery = useUsers();
  const { data: config } = useConfig();
  const updateUser = useUpdateUser();

  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(10);

  const [actionError, setActionError] = useState<string | null>(null);

  const isFiltered = Boolean(search.trim() || roleFilter || statusFilter);

  const filtered = useMemo(() => {
    const data = usersQuery.data ?? [];
    const term = search.trim().toLowerCase();
    return data.filter((u) => {
      if (
        term &&
        !u.email.toLowerCase().includes(term) &&
        !(u.full_name ?? "").toLowerCase().includes(term)
      ) {
        return false;
      }
      if (roleFilter && u.role !== roleFilter) return false;
      if (statusFilter === "active" && !u.is_active) return false;
      if (statusFilter === "inactive" && u.is_active) return false;
      return true;
    });
  }, [usersQuery.data, search, roleFilter, statusFilter]);

  const paged = useMemo(
    () => filtered.slice((page - 1) * perPage, page * perPage),
    [filtered, page, perPage],
  );

  const clearFilters = () => {
    setSearch("");
    setRoleFilter("");
    setStatusFilter("");
    setPage(1);
  };

  const toggleAccess = (user: User) => {
    setActionError(null);
    updateUser.mutate(
      { id: user.id, payload: { is_active: !user.is_active } },
      {
        onSuccess: () => toast.success(t("users:toast.updated")),
        onError: (error) =>
          setActionError(getErrorMessage(error, t("users:list.saveError"))),
      },
    );
  };
  const createButton = config ? (
    <a
      href={`${config.keycloak_url}/admin/${config.keycloak_realm}/console/`}
      target="_blank"
      rel="noopener noreferrer"
      className={buttonVariants({ variant: "default" })}
    >
      <ExternalLinkAltIcon data-icon="inline-start" />
      {t("users:identity.manageUsers")}
    </a>
  ) : undefined;

  return (
    <>
      <PageHeader
        title={t("users:list.title")}
        description={t("users:identity.listHelp")}
        actions={createButton}
      />
      <section className="page-section flex-1 min-h-0">
        <div className="flex flex-col gap-4">
          {actionError && (
            <div>
              <Alert
                variant="danger"
                isInline={true}
                isLiveRegion={true}
                title={actionError}
                actionClose={
                  <Button
                    aria-label={t("common:actions.close")}
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => setActionError(null)}
                  >
                    <XIcon data-icon="inline-start" />
                  </Button>
                }
              />
            </div>
          )}
          <div>
            <div id="users-toolbar" className="py-2">
              <div className="flex flex-wrap items-center gap-3">
                <div>
                  <SearchInput
                    aria-label={t("users:list.searchAria")}
                    placeholder={t("users:list.searchPlaceholder")}
                    value={search}
                    onChange={(_e, value) => {
                      setSearch(value);
                      setPage(1);
                    }}
                    onClear={() => setSearch("")}
                  />
                </div>
                <div>
                  <FilterSelect
                    aria-label={t("users:list.roleFilterAria")}
                    value={roleFilter}
                    onChange={(value) => {
                      setRoleFilter(value);
                      setPage(1);
                    }}
                    options={[
                      { value: "", label: t("users:list.allRoles") },
                      ...(Object.keys(ROLE_LABELS) as Role[]).map((role) => ({
                        value: role,
                        label: t(ROLE_LABELS[role]),
                      })),
                    ]}
                  />
                </div>
                <div>
                  <FilterSelect
                    aria-label={t("users:list.statusFilterAria")}
                    value={statusFilter}
                    onChange={(value) => {
                      setStatusFilter(value);
                      setPage(1);
                    }}
                    options={[
                      { value: "", label: t("users:list.allStatuses") },
                      {
                        value: "active",
                        label: t(ACTIVE_STATUS.active.labelKey),
                      },
                      {
                        value: "inactive",
                        label: t(ACTIVE_STATUS.inactive.labelKey),
                      },
                    ]}
                  />
                </div>
                <div className=" ml-auto">
                  <Pagination
                    itemCount={filtered.length}
                    page={page}
                    perPage={perPage}
                    onSetPage={(_e, p) => setPage(p)}
                    onPerPageSelect={(_e, pp) => {
                      setPerPage(pp);
                      setPage(1);
                    }}
                    isCompact={true}
                  />
                </div>
              </div>
            </div>
          </div>
          <div>
            <DataState
              isLoading={usersQuery.isLoading}
              error={usersQuery.error}
              onRetry={() => void usersQuery.refetch()}
              isEmpty={filtered.length === 0}
              emptyTitle={t("users:list.emptyTitle")}
              emptyBody={t("users:list.emptyBody")}
              emptyIcon={UsersIcon}
              emptyAction={createButton}
              isFiltered={isFiltered}
              onClearFilters={clearFilters}
              loadingLabel={t("users:list.loading")}
              skeleton={{ rows: 5 }}
            >
              <Table aria-label={t("users:list.tableAria")}>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("users:list.colEmail")}</TableHead>
                    <TableHead>{t("users:list.colFullName")}</TableHead>
                    <TableHead>{t("users:list.colRole")}</TableHead>
                    <TableHead>{t("users:list.colStatus")}</TableHead>
                    <TableHead>{t("users:list.colUserId")}</TableHead>
                    <TableHead>
                      <span className="sr-only">
                        {t("users:list.colActions")}
                      </span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paged.map((user) => (
                    <TableRow key={user.id}>
                      <TableCell>{user.email}</TableCell>
                      <TableCell>{user.full_name ?? "—"}</TableCell>
                      <TableCell>{t(ROLE_LABELS[user.role])}</TableCell>
                      <TableCell>
                        <StatusLabel
                          presentation={
                            ACTIVE_STATUS[
                              user.is_active ? "active" : "inactive"
                            ]
                          }
                          isCompact={true}
                        />
                      </TableCell>
                      <TableCell>
                        <CopyValue
                          aria-label={t("users:list.copyUserId", {
                            email: user.email,
                          })}
                        >
                          {user.id}
                        </CopyValue>
                      </TableCell>
                      <TableCell>
                        <Button
                          onClick={() => toggleAccess(user)}
                          aria-label={t(
                            user.is_active
                              ? "users:identity.suspendFor"
                              : "users:identity.restoreFor",
                            { email: user.email },
                          )}
                          disabled={
                            user.id === currentUser?.id || updateUser.isPending
                          }
                          variant="link"
                        >
                          {t(
                            user.is_active
                              ? "users:identity.suspend"
                              : "users:identity.restore",
                          )}
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </DataState>
          </div>
        </div>
      </section>
    </>
  );
}
