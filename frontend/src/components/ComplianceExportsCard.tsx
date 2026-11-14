import { DownloadIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import {
  TableHead,
  TableRow,
  TableHeader,
  TableCell,
  TableBody,
  Table,
} from "@/components/ui/table";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import {
  type ComplianceExport,
  downloadComplianceExport,
  listComplianceExports,
  requestComplianceExport,
} from "@/api/speakers";
import { Alert } from "@/components/Alert";
import { DataState } from "@/components/DataState";
import { StatusLabel } from "@/components/StatusLabel";
import { useToast } from "@/components/toast/useToast";
import { danger, info, type StatusPresentation, success } from "@/lib/labels";
import { downloadBlob } from "@/utils/downloadBlob";

const EXPORT_STATUS: Record<ComplianceExport["status"], StatusPresentation> = {
  pending: info("meetings:dsar.statuses.pending"),
  running: info("meetings:dsar.statuses.running", true),
  completed: success("meetings:dsar.statuses.completed"),
  failed: danger("meetings:dsar.statuses.failed"),
};

export function ComplianceExportsCard() {
  const { t } = useTranslation(["meetings", "common"]);
  const qc = useQueryClient();
  const toast = useToast();
  const [email, setEmail] = useState("");
  const exports = useQuery({
    queryKey: ["compliance-exports"],
    queryFn: listComplianceExports,
    refetchInterval: (query) =>
      (query.state.data as ComplianceExport[] | undefined)?.some(
        (job) => job.status === "pending" || job.status === "running",
      )
        ? 2000
        : false,
  });
  const request = useMutation({
    mutationFn: (subjectEmail: string) => requestComplianceExport(subjectEmail),
    onSettled: () => qc.invalidateQueries({ queryKey: ["compliance-exports"] }),
  });

  const download = async (job: ComplianceExport) => {
    try {
      const { blob, filename } = await downloadComplianceExport(job.id);
      downloadBlob(blob, filename ?? `dsar-${job.id}.zip`);
    } catch (error) {
      toast.error(t("common:dsar.downloadFailed"), getErrorMessage(error));
    }
  };

  const rows = exports.data ?? [];
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="section-title">{t("dsar.title")}</h2>
        <p className="app-copy">{t("dsar.subtitle")}</p>
      </div>
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <div>
            <Input
              type="email"
              value={email}
              aria-label={t("dsar.emailLabel")}
              placeholder={t("dsar.emailPlaceholder")}
              onChange={(_e) => {
                const v = _e.currentTarget.value;
                return setEmail(v);
              }}
            />
          </div>
          <div>
            <Button
              onClick={() =>
                request.mutate(email.trim(), {
                  onSuccess: () => {
                    setEmail("");
                    toast.success(t("common:dsar.requested"));
                  },
                })
              }
              disabled={!email.trim() || request.isPending}
              aria-busy={request.isPending}
              variant="default"
            >
              {request.isPending && (
                <Spinner
                  data-icon="inline-start"
                  aria-label={t("dsar.requesting")}
                />
              )}
              {t("dsar.request")}
            </Button>
          </div>
        </div>
      </div>
      {request.isError && (
        <div>
          <Alert
            variant="danger"
            isInline={true}
            isLiveRegion={true}
            title={t("dsar.requestFailed")}
          >
            {getErrorMessage(request.error)}
          </Alert>
        </div>
      )}
      <div>
        <DataState
          isLoading={exports.isLoading}
          error={exports.error}
          isEmpty={rows.length === 0}
          emptyIcon={DownloadIcon}
          emptyTitle={t("common:dsar.emptyTitle")}
          emptyBody={t("common:dsar.emptyBody")}
          onRetry={() => void exports.refetch()}
          headingLevel="h3"
        >
          <Table aria-label={t("dsar.title")}>
            <TableHeader>
              <TableRow>
                <TableHead>{t("dsar.subject")}</TableHead>
                <TableHead>{t("dsar.status")}</TableHead>
                <TableHead>{t("dsar.requestedBy")}</TableHead>
                <TableHead>
                  <span className="sr-only">{t("dsar.actionsLabel")}</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((job) => (
                <TableRow key={job.id}>
                  <TableCell>{job.subject_email}</TableCell>
                  <TableCell>
                    <StatusLabel
                      presentation={EXPORT_STATUS[job.status]}
                      value={job.status}
                      isCompact={true}
                    />
                    {job.status === "failed" && job.error && (
                      <small className="app-copy">{job.error}</small>
                    )}
                  </TableCell>
                  <TableCell>{job.requested_by_email}</TableCell>
                  <TableCell>
                    {job.status === "completed" && (
                      <Button
                        onClick={() => void download(job)}
                        variant="link"
                        size="sm"
                      >
                        {t("dsar.download")}
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
  );
}
