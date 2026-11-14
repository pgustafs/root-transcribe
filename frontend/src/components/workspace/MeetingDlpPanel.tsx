import { ShieldCheckIcon as ShieldAltIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Badge } from "@/components/ui/badge";
import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";

import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import type { DlpFinding } from "@/api/types";
import { Alert } from "@/components/Alert";
import { DataState } from "@/components/DataState";
import { StatusLabel } from "@/components/StatusLabel";
import { useToast } from "@/components/toast/useToast";
import {
  useApplyRedaction,
  useDlpFindings,
  useRequestDlpScan,
  useSetDlpFindingStatus,
} from "@/hooks/useMeetings";
import {
  danger,
  info,
  neutral,
  type StatusPresentation,
  success,
} from "@/lib/labels";
import { formatMsClock } from "@/utils/formatDuration";

const FINDING_STATUS: Record<DlpFinding["status"], StatusPresentation> = {
  pending: info("meetings:dlp.status.pending"),
  accepted: success("meetings:dlp.status.accepted"),
  redacting: info("meetings:dlp.status.redacting", true),
  dismissed: neutral("meetings:dlp.status.dismissed"),
  redacted: danger("meetings:dlp.status.redacted"),
};

export function MeetingDlpPanel({
  workspaceId,
  recordingId,
  onSeek,
}: {
  workspaceId: string;
  recordingId: string;
  /** Seek playback to a position in ms (finding time chips). */
  onSeek: (ms: number) => void;
}) {
  const { t } = useTranslation(["meetings", "common"]);
  const toast = useToast();
  const [confirmingApply, setConfirmingApply] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const onActionError = (fallbackKey: string) => (error: unknown) =>
    setActionError(getErrorMessage(error, t(fallbackKey)));
  const findings = useDlpFindings(workspaceId, recordingId, true);
  const setStatus = useSetDlpFindingStatus(workspaceId, recordingId);
  const scan = useRequestDlpScan(workspaceId, recordingId);
  const apply = useApplyRedaction(workspaceId, recordingId);

  const rows = findings.data ?? [];
  const acceptedCount = rows.filter((f) => f.status === "accepted").length;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <div>
            <small className="app-copy">{t("dlp.hint")}</small>
          </div>
          <div>
            <Button
              onClick={() => {
                setActionError(null);
                scan.mutate(undefined, {
                  onSuccess: () => toast.success(t("dlp.scanQueued")),
                  onError: onActionError("dlp.scanFailed"),
                });
              }}
              disabled={scan.isPending}
              variant="outline"
              size="sm"
            >
              {t("dlp.scan")}
            </Button>
          </div>
          <div>
            <Button
              onClick={() => setConfirmingApply(true)}
              disabled={acceptedCount === 0 || apply.isPending}
              aria-busy={apply.isPending}
              variant="destructive"
              size="sm"
            >
              {apply.isPending && (
                <Spinner
                  data-icon="inline-start"
                  aria-label={t("dlp.applying")}
                />
              )}
              {t("dlp.apply", { count: acceptedCount })}
            </Button>
          </div>
        </div>
      </div>
      <div>
        {/* Permanent polite region: the outcome is announced when it lands. */}
        <div aria-live="polite">
          {apply.isSuccess && (
            <Alert variant="info" isInline={true} title={t("dlp.applied")} />
          )}
        </div>
      </div>
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
        <DataState
          isLoading={findings.isLoading}
          error={findings.error}
          onRetry={() => void findings.refetch()}
          isEmpty={rows.length === 0}
          emptyTitle={t("dlp.emptyTitle")}
          emptyBody={t("dlp.emptyBody")}
          emptyIcon={ShieldAltIcon}
          headingLevel="h3"
        >
          <div className="flex flex-col gap-4">
            {rows.map((finding) => (
              <div key={finding.id}>
                <div className="flex flex-wrap items-center gap-2">
                  <div>
                    <Badge variant="secondary">
                      {finding.detector === "term" && finding.term_label
                        ? finding.term_label
                        : t(`dlp.detector.${finding.detector}`)}
                    </Badge>
                  </div>
                  <div>
                    <p className="app-copy">
                      <code>{finding.matched_text}</code>
                    </p>
                  </div>
                  <div>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => onSeek(finding.start_ms!)}
                    >
                      {formatMsClock(finding.start_ms)}
                    </Button>
                  </div>
                  <div>
                    <StatusLabel
                      presentation={FINDING_STATUS[finding.status]}
                      value={finding.status}
                      isCompact={true}
                    />
                  </div>
                  {(finding.status === "pending" ||
                    finding.status === "dismissed") && (
                    <div>
                      <Button
                        onClick={() =>
                          setStatus.mutate(
                            { findingId: finding.id, status: "accepted" },
                            { onError: onActionError("dlp.updateFailed") },
                          )
                        }
                        disabled={setStatus.isPending}
                        variant="link"
                        size="sm"
                      >
                        {t("dlp.accept")}
                      </Button>
                    </div>
                  )}
                  {(finding.status === "pending" ||
                    finding.status === "accepted") && (
                    <div>
                      <Button
                        onClick={() =>
                          setStatus.mutate(
                            { findingId: finding.id, status: "dismissed" },
                            { onError: onActionError("dlp.updateFailed") },
                          )
                        }
                        disabled={setStatus.isPending}
                        variant="link"
                        size="sm"
                      >
                        {t("dlp.dismiss")}
                      </Button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </DataState>
      </div>
      <Dialog
        open={confirmingApply}
        onOpenChange={(open) => {
          if (!open) (() => setConfirmingApply(false))();
        }}
      >
        <DialogContent
          aria-labelledby="dlp-apply-title"
          aria-describedby="dlp-apply-body"
          className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
        >
          <DialogHeader>
            <DialogTitle id="dlp-apply-title">
              {t("dlp.confirmTitle")}
            </DialogTitle>
          </DialogHeader>
          <div id="dlp-apply-body" className="min-w-0">
            {t("dlp.confirmBody", { count: acceptedCount })}
          </div>
          <DialogFooter>
            <Button
              onClick={() => {
                setActionError(null);
                apply.mutate(undefined, {
                  onSettled: () => setConfirmingApply(false),
                  onError: onActionError("dlp.applyFailed"),
                });
              }}
              disabled={apply.isPending}
              aria-busy={apply.isPending}
              variant="destructive"
            >
              {apply.isPending && (
                <Spinner data-icon="inline-start" aria-label={undefined} />
              )}
              {t("dlp.confirmApply")}
            </Button>
            <Button onClick={() => setConfirmingApply(false)} variant="link">
              {t("common:actions.cancel")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
