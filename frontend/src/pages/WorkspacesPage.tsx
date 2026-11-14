import { FolderOpenIcon, PlusIcon, XIcon } from "lucide-react";
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  CardFooter,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Badge } from "@/components/ui/badge";
import { RowActions } from "@/components/RowActions";
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
import { Link, useNavigate } from "react-router-dom";

import { getErrorMessage } from "@/api/client";
import type { Workspace } from "@/api/types";
import { Alert } from "@/components/Alert";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { DataState } from "@/components/DataState";
import { RecordingGuide } from "@/components/RecordingGuide";
import { PageHeader } from "@/components/PageHeader";
import { Pagination } from "@/components/Pagination";
import { SearchInput } from "@/components/SearchInput";
import { useToast } from "@/components/toast/useToast";
import {
  CreateWorkspaceModal,
  type CreateWorkspacePayload,
} from "@/components/workspace/CreateWorkspaceModal";
import {
  useCreateWorkspace,
  useDeleteWorkspace,
  useMyWorkspaces,
} from "@/hooks/useWorkspaces";
import { WORKSPACE_ROLE_LABELS } from "@/lib/labels";

export function WorkspacesPage() {
  const { t } = useTranslation(["access", "common"]);
  const navigate = useNavigate();
  const toast = useToast();
  const workspacesQuery = useMyWorkspaces();
  const createWorkspace = useCreateWorkspace();
  const deleteWorkspace = useDeleteWorkspace();

  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(10);
  const [isCreateOpen, setCreateOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [toDelete, setToDelete] = useState<Workspace | null>(null);

  const isFiltered = Boolean(search.trim());
  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    const data = workspacesQuery.data ?? [];
    return term
      ? data.filter((w) => w.name.toLowerCase().includes(term))
      : data;
  }, [workspacesQuery.data, search]);
  const paged = useMemo(
    () => filtered.slice((page - 1) * perPage, page * perPage),
    [filtered, page, perPage],
  );

  const clearFilters = () => {
    setSearch("");
    setPage(1);
  };

  const openCreate = () => {
    setCreateError(null);
    setCreateOpen(true);
  };

  const submitCreate = (payload: CreateWorkspacePayload) => {
    setCreateError(null);
    createWorkspace.mutate(payload, {
      onSuccess: (ws) => {
        setCreateOpen(false);
        toast.success(t("access:workspaces.toast.created", { name: ws.name }));
        navigate(`/workspaces/${ws.id}/meetings`);
      },
      onError: (error) =>
        setCreateError(
          getErrorMessage(error, t("access:workspaces.createError")),
        ),
    });
  };

  const confirmDelete = () => {
    if (!toDelete) return;
    const { id, name } = toDelete;
    deleteWorkspace.mutate(id, {
      onSuccess: () => {
        setToDelete(null);
        toast.success(t("access:workspaces.toast.deleted", { name }));
      },
      onError: (error) => {
        setToDelete(null);
        setActionError(
          getErrorMessage(error, t("access:workspaces.deleteError")),
        );
      },
    });
  };

  const createButton = (
    <Button onClick={openCreate} variant="default">
      <PlusIcon data-icon="inline-start" />
      {t("access:workspaces.create")}
    </Button>
  );

  return (
    <>
      <PageHeader
        title={t("access:workspaces.title")}
        description={t("access:workspaces.subtitle")}
        actions={createButton}
      />
      <section className="page-section flex-1 min-h-0">
        <div className="flex flex-col gap-4">
          {!workspacesQuery.isLoading &&
            !workspacesQuery.isError &&
            workspacesQuery.data?.length === 0 && (
              <div>
                <RecordingGuide />
              </div>
            )}
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
          <Card className="gap-0 overflow-hidden py-0">
            <CardHeader className="p-4">
              <CardTitle className="sr-only">
                {t("access:workspaces.tableAria")}
              </CardTitle>
              <div
                id="workspaces-toolbar"
                className="flex flex-wrap items-center gap-3"
              >
                <SearchInput
                  className="max-w-sm"
                  aria-label={t("access:workspaces.searchAria")}
                  placeholder={t("access:workspaces.searchPlaceholder")}
                  value={search}
                  onChange={(_e, value) => {
                    setSearch(value);
                    setPage(1);
                  }}
                  onClear={clearFilters}
                />
              </div>
            </CardHeader>
            <Separator />
            <CardContent className="px-0">
              <DataState
                isLoading={workspacesQuery.isLoading}
                error={workspacesQuery.error}
                onRetry={() => void workspacesQuery.refetch()}
                isEmpty={filtered.length === 0}
                emptyTitle={t("access:workspaces.empty")}
                emptyBody={t("access:workspaces.emptyBody")}
                emptyIcon={FolderOpenIcon}
                emptyAction={createButton}
                isFiltered={isFiltered}
                onClearFilters={clearFilters}
                loadingLabel={t("access:workspaces.loading")}
                skeleton={{ rows: 5 }}
              >
                <Table aria-label={t("access:workspaces.tableAria")}>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("access:workspaces.name")}</TableHead>
                      <TableHead>{t("access:workspaces.colRole")}</TableHead>
                      <TableHead>
                        {t("access:workspaces.colVisibility")}
                      </TableHead>
                      <TableHead>
                        <span className="sr-only">
                          {t("access:workspaces.colActions")}
                        </span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {paged.map((w) => (
                      <TableRow key={w.id}>
                        <TableCell className="max-w-80 py-4 pl-4">
                          <Link
                            className="flex items-center gap-2 font-medium underline-offset-4 hover:underline"
                            to={`/workspaces/${w.id}/meetings`}
                          >
                            <FolderOpenIcon className="size-4 shrink-0 text-muted-foreground" />
                            <span className="truncate">{w.name}</span>
                          </Link>
                          {w.description && (
                            <p className="mt-1 ml-6 max-w-md whitespace-normal text-xs text-muted-foreground">
                              {w.description}
                            </p>
                          )}
                        </TableCell>
                        <TableCell>
                          {w.my_role
                            ? t(WORKSPACE_ROLE_LABELS[w.my_role])
                            : "—"}
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">
                            {t(`access:workspaces.visibility.${w.visibility}`)}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          {/* Delete is owner-only (admins are implicit owners, so
                            the backend reports my_role="owner" for them). */}
                          {w.my_role === "owner" && (
                            <RowActions
                              toggleAriaLabel={t("common:rowActionsAria")}
                              items={[
                                {
                                  title: t("common:actions.delete"),
                                  isDanger: true,
                                  onClick: () => setToDelete(w),
                                },
                              ]}
                            />
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </DataState>
            </CardContent>
            <Separator />
            <CardFooter className="justify-end p-3">
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
            </CardFooter>
          </Card>
        </div>
      </section>

      {isCreateOpen && (
        <CreateWorkspaceModal
          submitError={createError}
          isSubmitting={createWorkspace.isPending}
          onSubmit={submitCreate}
          onClose={() => setCreateOpen(false)}
        />
      )}

      {toDelete && (
        <ConfirmDeleteModal
          isOpen={true}
          itemName={toDelete.name}
          title={t("access:workspaces.deleteTitle")}
          consequence={t("access:workspaces.deleteBody")}
          isDeleting={deleteWorkspace.isPending}
          onConfirm={confirmDelete}
          onClose={() => setToDelete(null)}
        />
      )}
    </>
  );
}
