import {
  PlusIcon,
  ShieldCheckIcon as ShieldAltIcon,
  XIcon,
} from "lucide-react";
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

import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import type { DlpFinding } from "@/api/types";
import { Alert } from "@/components/Alert";
import { DataState } from "@/components/DataState";
import { StatusLabel } from "@/components/StatusLabel";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  useAddDlpTerm,
  useApplyRedaction,
  useDeleteDlpTerm,
  useDlpFindings,
  useDlpTerms,
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

/** How long the list is re-read after a scan was requested. */
const SCAN_WATCH_MS = 8000;
const BUILT_IN = ["email", "phone", "card", "secret"] as const;

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
  const [confirmingApply, setConfirmingApply] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const onActionError = (fallbackKey: string) => (error: unknown) =>
    setActionError(getErrorMessage(error, t(fallbackKey)));
  // The scan runs in the background and reports nothing back, so the list is
  // re-read for a few seconds after one was requested.
  const [scanning, setScanning] = useState(false);
  const [scanned, setScanned] = useState(false);
  useEffect(() => {
    if (!scanning) return;
    const timer = window.setTimeout(() => {
      setScanning(false);
      setScanned(true);
    }, SCAN_WATCH_MS);
    return () => window.clearTimeout(timer);
  }, [scanning]);
  const findings = useDlpFindings(workspaceId, recordingId, true, scanning);
  const setStatus = useSetDlpFindingStatus(workspaceId, recordingId);
  const scan = useRequestDlpScan(workspaceId, recordingId);
  const terms = useDlpTerms(workspaceId);
  const addTerm = useAddDlpTerm(workspaceId);
  const removeTerm = useDeleteDlpTerm(workspaceId);
  const [word, setWord] = useState("");
  const [isRegex, setIsRegex] = useState(false);

  const startScan = () => {
    setActionError(null);
    scan.mutate(undefined, {
      onSuccess: () => setScanning(true),
      onError: onActionError("dlp.scanFailed"),
    });
  };
  const submitWord = (event: FormEvent) => {
    event.preventDefault();
    const pattern = word.trim();
    if (!pattern) return;
    setActionError(null);
    addTerm.mutate(
      { pattern, is_regex: isRegex, label: isRegex ? null : pattern },
      {
        onSuccess: () => {
          setWord("");
          startScan();
        },
        onError: onActionError("dlp.terms.addFailed"),
      },
    );
  };
  const apply = useApplyRedaction(workspaceId, recordingId);

  const rows = findings.data ?? [];
  const acceptedCount = rows.filter((f) => f.status === "accepted").length;

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">{t("dlp.hint")}</p>
      <section
        aria-labelledby="dlp-terms-title"
        className="flex flex-col gap-3 rounded-lg border p-4"
      >
        <div>
          <h3 id="dlp-terms-title" className="text-sm font-medium">
            {t("dlp.terms.title")}
          </h3>
          <p className="text-sm text-muted-foreground">{t("dlp.terms.hint")}</p>
        </div>
        <ul className="flex flex-wrap gap-2" aria-label={t("dlp.terms.title")}>
          {BUILT_IN.map((detector) => (
            <li key={detector}>
              <Badge variant="secondary">
                {t(`dlp.terms.builtin.${detector}`)}
              </Badge>
            </li>
          ))}
          {(terms.data ?? []).map((term) => (
            <li key={term.id}>
              <Badge variant="outline" className="gap-1 pr-1">
                {term.is_regex ? <code>{term.pattern}</code> : term.pattern}
                <Button
                  variant="ghost"
                  size="icon-xs"
                  className="size-4"
                  disabled={removeTerm.isPending}
                  aria-label={t("dlp.terms.remove", { word: term.pattern })}
                  onClick={() =>
                    removeTerm.mutate(term.id, {
                      onSuccess: startScan,
                      onError: onActionError("dlp.terms.removeFailed"),
                    })
                  }
                >
                  <XIcon />
                </Button>
              </Badge>
            </li>
          ))}
        </ul>
        <form
          onSubmit={submitWord}
          className="flex flex-wrap items-center gap-x-3 gap-y-2"
        >
          <Label htmlFor="dlp-word" className="sr-only">
            {t("dlp.terms.wordLabel")}
          </Label>
          <Input
            id="dlp-word"
            value={word}
            maxLength={500}
            placeholder={t("dlp.terms.wordPlaceholder")}
            className="w-full sm:w-72"
            onChange={(event) => setWord(event.currentTarget.value)}
          />
          <Button
            type="submit"
            variant="outline"
            size="sm"
            disabled={!word.trim() || addTerm.isPending}
          >
            <PlusIcon data-icon="inline-start" />
            {t("dlp.terms.add")}
          </Button>
          <div className="flex items-center gap-2">
            <Checkbox
              id="dlp-word-regex"
              checked={isRegex}
              onCheckedChange={(checked) => setIsRegex(checked === true)}
            />
            <Label htmlFor="dlp-word-regex" className="font-normal">
              {t("dlp.terms.regex")}
            </Label>
          </div>
        </form>
      </section>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          onClick={startScan}
          disabled={scan.isPending || scanning}
          aria-busy={scanning}
          variant="outline"
          size="sm"
        >
          {scanning && <Spinner data-icon="inline-start" aria-hidden="true" />}
          {t(scanning ? "dlp.scanning" : "dlp.scan")}
        </Button>
        <Button
          onClick={() => setConfirmingApply(true)}
          disabled={acceptedCount === 0 || apply.isPending}
          aria-busy={apply.isPending}
          variant="destructive"
          size="sm"
        >
          {apply.isPending && (
            <Spinner data-icon="inline-start" aria-label={t("dlp.applying")} />
          )}
          {t("dlp.apply", { count: acceptedCount })}
        </Button>
        <span className="text-sm text-muted-foreground" aria-live="polite">
          {scanned && !scanning && t("dlp.scanDone", { count: rows.length })}
        </span>
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
          emptyBody={t(scanned ? "dlp.emptyBodyScanned" : "dlp.emptyBody")}
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
