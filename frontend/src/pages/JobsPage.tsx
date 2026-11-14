import {
  RefreshCwIcon as SyncAltIcon,
  ListTodoIcon as TasksIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  TableHead,
  TableRow,
  TableHeader,
  TableCell,
  TableBody,
  Table,
} from "@/components/ui/table";
import { Checkbox } from "@/components/ui/checkbox";
import { CopyValue } from "@/components/CopyValue";
import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import i18n from "@/i18n";
import { getErrorMessage } from "@/api/client";
import type { Job, JobStatus } from "@/api/types";
import { FilterSelect } from "@/components/FilterSelect";
import { Alert } from "@/components/Alert";
import { DataState } from "@/components/DataState";
import { PageHeader } from "@/components/PageHeader";
import { SearchInput } from "@/components/SearchInput";
import { StatCard } from "@/components/StatCard";
import { StatusLabel } from "@/components/StatusLabel";
import { useToast } from "@/components/toast/useToast";
import { useCancelJob, useJobs, useJobsSummary } from "@/hooks/useJobs";
import { JOB_STATUS } from "@/lib/labels";

const STATUSES: JobStatus[] = [
  "pending",
  "started",
  "success",
  "failure",
  "retry",
  "revoked",
];

// Non-terminal states whose task can still be cancelled (mirrors the backend).
const CANCELLABLE: ReadonlySet<string> = new Set([
  "pending",
  "started",
  "retry",
]);

function isCancellable(job: Job): boolean {
  return CANCELLABLE.has(job.status);
}

function formatDuration(ms: number | null, t: TFunction): string {
  if (ms === null || ms === undefined) return "—";
  const number = new Intl.NumberFormat(i18n.language, {
    maximumFractionDigits: 1,
  });
  if (ms < 1000) return t("jobs:duration.ms", { value: number.format(ms) });
  if (ms < 60_000) {
    return t("jobs:duration.s", { value: number.format(ms / 1000) });
  }
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return t("jobs:duration.min", { minutes, seconds });
}

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(i18n.language);
}

export function JobsPage() {
  const { t } = useTranslation(["jobs", "common"]);
  const toast = useToast();
  const [status, setStatus] = useState<JobStatus | "">("");
  const [taskName, setTaskName] = useState("");
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(50);
  const [detail, setDetail] = useState<Job | null>(null);

  const params = useMemo(
    () => ({
      status: status || undefined,
      task_name: taskName || undefined,
      q: search.trim() || undefined,
      limit,
    }),
    [status, taskName, search, limit],
  );

  const jobsQuery = useJobs(params);
  const summary = useJobsSummary();
  const jobs = useMemo(() => jobsQuery.data ?? [], [jobsQuery.data]);

  // Distinct task names for the filter, drawn from the rows currently loaded.
  const taskNames = useMemo(
    () => Array.from(new Set(jobs.map((j) => j.task_name))).sort(),
    [jobs],
  );

  // --- Cancel (single + bulk) ---
  const cancelJob = useCancelJob();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Pending confirmation: the task ids to cancel (1 for a row, N for bulk).
  const [confirmIds, setConfirmIds] = useState<string[] | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const cancellableJobs = useMemo(() => jobs.filter(isCancellable), [jobs]);
  // Keep the selection in sync with what's still cancellable in the current rows.
  const selectedCancellable = useMemo(
    () => cancellableJobs.filter((j) => selected.has(j.task_id)),
    [cancellableJobs, selected],
  );
  const allSelected =
    cancellableJobs.length > 0 &&
    selectedCancellable.length === cancellableJobs.length;

  const toggleRow = (taskId: string, isSelecting: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (isSelecting) next.add(taskId);
      else next.delete(taskId);
      return next;
    });

  const toggleAll = (isSelecting: boolean) =>
    setSelected(
      isSelecting ? new Set(cancellableJobs.map((j) => j.task_id)) : new Set(),
    );

  const confirmCancel = async () => {
    if (!confirmIds) return;
    setIsCancelling(true);
    setCancelError(null);
    try {
      // Sequential, like the bulk-delete pattern — each cancel is audited.
      for (const taskId of confirmIds) {
        await cancelJob.mutateAsync(taskId);
      }
      setSelected(new Set());
      setConfirmIds(null);
      toast.success(t("jobs:cancel.toast", { count: confirmIds.length }));
    } catch (err) {
      setCancelError(getErrorMessage(err, t("jobs:cancel.error")));
    } finally {
      setIsCancelling(false);
    }
  };

  const isFiltered = Boolean(status || taskName || search.trim());
  const clearFilters = () => {
    setStatus("");
    setTaskName("");
    setSearch("");
  };

  const running = (summary.data?.pending ?? 0) + (summary.data?.started ?? 0);

  const cards: { key: string; label: string; value: number }[] = [
    {
      key: "total",
      label: t("jobs:cards.total"),
      value: summary.data?.total ?? 0,
    },
    { key: "running", label: t("jobs:cards.running"), value: running },
    {
      key: "failure",
      label: t("jobs:cards.failed"),
      value: summary.data?.failure ?? 0,
    },
  ];

  return (
    <>
      <PageHeader
        title={t("jobs:title")}
        description={t("jobs:description")}
        actions={
          <Button
            onClick={() => void jobsQuery.refetch()}
            disabled={jobsQuery.isFetching}
            variant="outline"
          >
            {<SyncAltIcon data-icon="inline-start" />}
            {t("jobs:filters.refresh")}
          </Button>
        }
      />
      <section className="page-section flex-1 min-h-0">
        <div className="flex flex-col gap-4">
          <div>
            <div className="grid grid-cols-3 gap-2 sm:gap-4">
              {cards.map((card) => (
                <StatCard
                  key={card.key}
                  title={card.label}
                  value={card.value}
                  isLoading={summary.isLoading}
                  loadingLabel={t("jobs:cards.loading")}
                />
              ))}
            </div>
          </div>
          <div>
            <div id="jobs-toolbar" className="py-2">
              <div className="flex flex-wrap items-center gap-3">
                <div>
                  <SearchInput
                    aria-label={t("jobs:filters.searchAria")}
                    placeholder={t("jobs:filters.searchPlaceholder")}
                    value={search}
                    onChange={(_e, v) => setSearch(v)}
                    onClear={() => setSearch("")}
                  />
                </div>
                <div>
                  <FilterSelect
                    aria-label={t("jobs:filters.statusAria")}
                    value={status}
                    onChange={(v) => setStatus(v as JobStatus | "")}
                    options={[
                      { value: "", label: t("jobs:filters.statusAll") },
                      ...STATUSES.map((s) => ({
                        value: s,
                        label: t(JOB_STATUS[s].labelKey),
                      })),
                    ]}
                  />
                </div>
                <div>
                  <FilterSelect
                    aria-label={t("jobs:filters.taskAria")}
                    value={taskName}
                    onChange={setTaskName}
                    options={[
                      { value: "", label: t("jobs:filters.taskAll") },
                      ...taskNames.map((name) => ({
                        value: name,
                        label: name,
                      })),
                    ]}
                  />
                </div>
                <div>
                  <FilterSelect
                    aria-label={t("jobs:filters.limitAria")}
                    value={String(limit)}
                    onChange={(v) => setLimit(Number(v))}
                    options={[25, 50, 100, 200].map((n) => ({
                      value: String(n),
                      label: t("jobs:filters.limitOption", { count: n }),
                    }))}
                  />
                </div>
                {selectedCancellable.length > 0 && (
                  <div className=" ml-auto">
                    <Button
                      onClick={() =>
                        setConfirmIds(selectedCancellable.map((j) => j.task_id))
                      }
                      variant="destructive"
                    >
                      {t("jobs:cancel.bulk", {
                        count: selectedCancellable.length,
                      })}
                    </Button>
                  </div>
                )}
              </div>
            </div>
          </div>
          <div>
            <DataState
              isLoading={jobsQuery.isLoading}
              error={jobsQuery.error}
              onRetry={() => void jobsQuery.refetch()}
              isEmpty={jobs.length === 0}
              emptyTitle={t("jobs:emptyTitle")}
              emptyBody={t("jobs:emptyBody")}
              emptyIcon={TasksIcon}
              isFiltered={isFiltered}
              onClearFilters={clearFilters}
              loadingLabel={t("jobs:loading")}
              skeleton={{ rows: 5 }}
            >
              <Table aria-label={t("jobs:tableAria")}>
                <TableHeader>
                  <TableRow>
                    <TableHead>
                      <Checkbox
                        checked={allSelected}
                        disabled={cancellableJobs.length === 0}
                        onCheckedChange={(value) => {
                          const isSelecting = value === true;
                          toggleAll(isSelecting);
                        }}
                        aria-label={t("jobs:cancel.selectAllAria")}
                      />
                    </TableHead>
                    <TableHead>{t("jobs:col.task")}</TableHead>
                    <TableHead>{t("jobs:col.status")}</TableHead>
                    <TableHead>{t("jobs:col.triggeredBy")}</TableHead>
                    <TableHead>{t("jobs:col.started")}</TableHead>
                    <TableHead>{t("jobs:col.duration")}</TableHead>
                    <TableHead>{t("jobs:col.taskId")}</TableHead>
                    <TableHead>{t("jobs:col.requestId")}</TableHead>
                    <TableHead>
                      <span className="sr-only">{t("jobs:col.actions")}</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {jobs.map((job) => (
                    <TableRow key={job.task_id}>
                      <TableCell>
                        <Checkbox
                          checked={selected.has(job.task_id)}
                          disabled={!isCancellable(job)}
                          onCheckedChange={(value) => {
                            const isSelecting = value === true;
                            toggleRow(job.task_id, isSelecting);
                          }}
                          aria-label={t("jobs:cancel.selectRowAria", {
                            name: job.task_name,
                            id: job.task_id,
                          })}
                        />
                      </TableCell>
                      <TableCell>
                        <Button onClick={() => setDetail(job)} variant="link">
                          {job.task_name}
                        </Button>
                      </TableCell>
                      <TableCell>
                        <StatusLabel
                          presentation={JOB_STATUS[job.status]}
                          value={job.status}
                          isCompact={true}
                        />
                      </TableCell>
                      <TableCell>{job.triggered_by ?? "—"}</TableCell>
                      <TableCell>
                        {formatTime(job.started_at ?? job.created_at)}
                      </TableCell>
                      <TableCell>{formatDuration(job.runtime_ms, t)}</TableCell>
                      <TableCell>
                        <CopyValue
                          aria-label={t("jobs:copyTaskId", { id: job.task_id })}
                        >
                          {job.task_id}
                        </CopyValue>
                      </TableCell>
                      <TableCell>
                        {job.request_id ? (
                          <CopyValue
                            aria-label={t("jobs:copyRequestId", {
                              id: job.request_id,
                            })}
                          >
                            {job.request_id}
                          </CopyValue>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell>
                        {isCancellable(job) && (
                          <Button
                            aria-label={t("jobs:cancel.actionFor", {
                              task: job.task_name,
                            })}
                            onClick={() => setConfirmIds([job.task_id])}
                            variant="destructive"
                          >
                            {t("jobs:cancel.action")}
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </DataState>
          </div>
        </div>
      </section>

      {detail && (
        <JobDetailModal job={detail} onClose={() => setDetail(null)} />
      )}

      {confirmIds && (
        <ConfirmCancelModal
          count={confirmIds.length}
          isCancelling={isCancelling}
          error={cancelError}
          onConfirm={confirmCancel}
          onClose={() => {
            setConfirmIds(null);
            setCancelError(null);
          }}
        />
      )}
    </>
  );
}

function ConfirmCancelModal({
  count,
  isCancelling,
  error,
  onConfirm,
  onClose,
}: {
  count: number;
  isCancelling: boolean;
  error: string | null;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation(["jobs", "common"]);
  return (
    <Dialog
      open={true}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        aria-labelledby="confirm-cancel-title"
        aria-describedby="confirm-cancel-body"
        className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle id="confirm-cancel-title">
            {t("jobs:cancel.confirmTitle", { count })}
          </DialogTitle>
        </DialogHeader>
        <div id="confirm-cancel-body" className="min-w-0">
          <div className="flex flex-col gap-4">
            <div>
              <p className="app-copy">
                {t("jobs:cancel.confirmBody", { count })}
              </p>
            </div>
            {error && (
              <div>
                <Alert
                  variant="danger"
                  isInline={true}
                  isLiveRegion={true}
                  title={error}
                />
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button
            onClick={onConfirm}
            disabled={isCancelling}
            aria-busy={isCancelling}
            variant="destructive"
          >
            {isCancelling && (
              <Spinner data-icon="inline-start" aria-label={undefined} />
            )}
            {t("jobs:cancel.confirmAction", { count })}
          </Button>
          <Button onClick={onClose} disabled={isCancelling} variant="link">
            {t("jobs:cancel.dismiss")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function JobDetailModal({ job, onClose }: { job: Job; onClose: () => void }) {
  const { t } = useTranslation(["jobs", "common"]);
  const dash = "—";
  const rows: { term: string; value: string }[] = [
    { term: t("jobs:detail.taskName"), value: job.task_name },
    { term: t("jobs:detail.taskId"), value: job.task_id },
    { term: t("jobs:detail.requestId"), value: job.request_id ?? dash },
    { term: t("jobs:detail.triggeredBy"), value: job.triggered_by ?? dash },
    { term: t("jobs:detail.worker"), value: job.worker ?? dash },
    { term: t("jobs:detail.queue"), value: job.queue ?? dash },
    { term: t("jobs:detail.retries"), value: String(job.retries) },
    { term: t("jobs:detail.created"), value: formatTime(job.created_at) },
    { term: t("jobs:detail.started"), value: formatTime(job.started_at) },
    { term: t("jobs:detail.finished"), value: formatTime(job.finished_at) },
    {
      term: t("jobs:detail.duration"),
      value: formatDuration(job.runtime_ms, t),
    },
    { term: t("jobs:detail.args"), value: job.args_summary ?? dash },
    { term: t("jobs:detail.result"), value: job.result_summary ?? dash },
  ];

  return (
    <Dialog
      open={true}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        aria-labelledby="job-detail-title"
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl"
      >
        <DialogHeader>
          <DialogTitle id="job-detail-title">
            {t("jobs:detail.title")}
          </DialogTitle>
        </DialogHeader>
        <div className="min-w-0">
          <div className="flex flex-col gap-4">
            <div>
              <StatusLabel
                presentation={JOB_STATUS[job.status]}
                value={job.status}
              />
            </div>
            <div>
              <dl className="grid gap-3 sm:grid-cols-2">
                {rows.map((row) => (
                  <div key={row.term} className="min-w-0">
                    <dt className="text-sm text-muted-foreground">
                      {row.term}
                    </dt>
                    <dd className={cn("break-words", "wrap-anywhere")}>
                      {row.value}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
            {job.error && (
              <div>
                <div className="flex flex-col gap-4">
                  <h2 className="section-title">{t("jobs:detail.error")}</h2>
                  {/* Focusable so keyboard users can scroll a long message. */}
                  <pre
                    aria-label={t("jobs:detail.errorRegion")}
                    className="overflow-auto rounded-md bg-muted p-4 text-xs"
                  >
                    <code>{job.error}</code>
                  </pre>
                </div>
              </div>
            )}
            {job.traceback && (
              <div>
                <div className="flex flex-col gap-4">
                  <h2 className="section-title">
                    {t("jobs:detail.traceback")}
                  </h2>
                  <pre
                    aria-label={t("jobs:detail.tracebackRegion")}
                    className="overflow-auto rounded-md bg-muted p-4 text-xs"
                  >
                    <code>{job.traceback}</code>
                  </pre>
                </div>
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button onClick={onClose} variant="link">
            {t("common:actions.close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
