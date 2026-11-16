/** The "Cleaned" tab: the transcript with the language model's corrections.
 *
 * The transcript itself is never changed. A corrected line shows what was
 * replaced and what replaced it, and a person can keep the original instead.
 * Lines, speakers and times are the transcript's own. */

import { SparklesIcon, XIcon } from "lucide-react";
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";

import type { CleanupChange } from "@/api/cleanup";
import { getErrorMessage } from "@/api/client";
import type { MeetingSegment } from "@/api/types";
import { Alert } from "@/components/Alert";
import { StatusLabel } from "@/components/StatusLabel";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import {
  useCleanup,
  useCreateCleanup,
  useDecideCleanupChange,
  useDeleteCleanup,
} from "@/hooks/useCleanup";
import { neutral, success } from "@/lib/labels";
import { formatMsClock } from "@/utils/formatDuration";
import { speakerLabel } from "@/utils/speakerLabel";
import { diffWords } from "@/utils/wordDiff";

const CORRECTED = success("meetings:cleanup.corrected");
const ORIGINAL_KEPT = neutral("meetings:cleanup.originalKept");

export function MeetingCleanedPanel({
  workspaceId,
  recordingId,
  segments,
  canCreate,
  isAvailable,
  onSeek,
}: {
  workspaceId: string;
  recordingId: string;
  /** The transcript on display: the lines the corrections are laid over. */
  segments: MeetingSegment[];
  canCreate: boolean;
  /** A language model is configured. */
  isAvailable: boolean;
  onSeek: (ms: number) => void;
}) {
  const { t } = useTranslation(["meetings", "common"]);
  const cleanup = useCleanup(workspaceId, recordingId, true);
  const create = useCreateCleanup(workspaceId, recordingId);
  const decide = useDecideCleanupChange(workspaceId, recordingId);
  const remove = useDeleteCleanup(workspaceId, recordingId);
  const [onlyCorrected, setOnlyCorrected] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const onError = (fallbackKey: string) => (error: unknown) =>
    setActionError(getErrorMessage(error, t(fallbackKey)));

  const current = cleanup.data ?? null;
  const working =
    current?.status === "pending" || current?.status === "processing";
  const start = () => {
    setActionError(null);
    create.mutate(undefined, { onError: onError("cleanup.startFailed") });
  };

  // A correction applies only while the line still reads as the model saw it:
  // a line edited since then shows as it is now.
  const changes = new Map<string, CleanupChange>();
  if (current?.status === "ready") {
    const texts = new Map(segments.map((s) => [s.id, s.text]));
    for (const change of current.changes)
      if (texts.get(change.segment_id) === change.original)
        changes.set(change.segment_id, change);
  }
  const used = [...changes.values()].filter(
    (c) => c.status === "suggested",
  ).length;
  const rows = onlyCorrected
    ? segments.filter((s) => changes.has(s.id))
    : segments;
  // "part:2/7" → 2 of 7.
  const step = /^part:(\d+)\/(\d+)$/.exec(current?.current_step ?? "");

  return (
    <div className="cleanup-panel">
      <p className="text-sm text-muted-foreground">{t("cleanup.hint")}</p>
      {!isAvailable && (
        <Alert
          variant="info"
          isInline={true}
          title={t("cleanup.unavailableTitle")}
        >
          {t("cleanup.unavailableBody")}
        </Alert>
      )}
      {actionError && (
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
      )}
      {current?.status === "failed" && (
        <Alert
          variant="danger"
          isInline={true}
          title={t("cleanup.failedTitle")}
        >
          {current.error}
        </Alert>
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {canCreate && isAvailable && (
          <Button
            onClick={start}
            disabled={working || create.isPending}
            aria-busy={working}
            variant={current?.status === "ready" ? "outline" : "default"}
            size="sm"
          >
            {working ? (
              <Spinner data-icon="inline-start" aria-hidden="true" />
            ) : (
              <SparklesIcon data-icon="inline-start" />
            )}
            {t(
              working
                ? "cleanup.working"
                : current
                  ? "cleanup.again"
                  : "cleanup.create",
            )}
          </Button>
        )}
        {canCreate && current && !working && (
          <Button
            onClick={() => {
              setActionError(null);
              remove.mutate(current.id, {
                onError: onError("cleanup.removeFailed"),
              });
            }}
            disabled={remove.isPending}
            variant="ghost"
            size="sm"
          >
            {t("cleanup.remove")}
          </Button>
        )}
        {/* Permanent polite region: progress and the outcome are announced. */}
        <span className="text-sm text-muted-foreground" aria-live="polite">
          {working &&
            (step
              ? t("cleanup.progress", { part: step[1], parts: step[2] })
              : t("cleanup.queued"))}
          {current?.status === "ready" &&
            t("cleanup.summary", { count: changes.size }) +
              (current.rejected > 0
                ? ` · ${t("cleanup.leftOut", { count: current.rejected })}`
                : "") +
              (current.model ? ` · ${current.model}` : "")}
        </span>
      </div>

      {current?.status === "ready" && changes.size > 0 && (
        <div className="flex items-center gap-2">
          <Checkbox
            id="cleanup-only-corrected"
            checked={onlyCorrected}
            onCheckedChange={(state) => setOnlyCorrected(state === true)}
          />
          <Label htmlFor="cleanup-only-corrected" className="font-normal">
            {t("cleanup.onlyCorrected", { used, count: changes.size })}
          </Label>
        </div>
      )}

      {current?.status === "ready" && (
        <div className="transcript-document">
          {rows.map((segment) => {
            const change = changes.get(segment.id);
            const name =
              segment.speaker_name ??
              speakerLabel(segment.speaker_label, (number) =>
                t("review.speaker", { number }),
              );
            const isUsed = change?.status === "suggested";
            return (
              <article
                key={segment.id}
                className={
                  !change
                    ? "meeting-seg"
                    : isUsed
                      ? "meeting-seg cleanup-seg--changed"
                      : "meeting-seg cleanup-seg--kept"
                }
              >
                <div className="flex flex-wrap items-center gap-1">
                  <Button
                    onClick={() => onSeek(segment.start_ms)}
                    aria-label={t("detail.seekTo", {
                      ts: formatMsClock(segment.start_ms),
                    })}
                    variant="link"
                    size="sm"
                  >
                    {formatMsClock(segment.start_ms)}
                  </Button>
                  <p className="app-copy">
                    <strong>{name}</strong>
                  </p>
                  {change && (
                    <StatusLabel
                      presentation={isUsed ? CORRECTED : ORIGINAL_KEPT}
                      isCompact={true}
                    />
                  )}
                  {change && canCreate && (
                    <div className="ml-auto">
                      <Button
                        onClick={() => {
                          setActionError(null);
                          decide.mutate(
                            {
                              cleanupId: current.id,
                              segmentId: segment.id,
                              decision: isUsed ? "dismiss" : "use",
                            },
                            { onError: onError("cleanup.decideFailed") },
                          );
                        }}
                        disabled={decide.isPending}
                        variant="ghost"
                        size="sm"
                      >
                        {t(
                          isUsed
                            ? "cleanup.keepOriginal"
                            : "cleanup.useCorrection",
                        )}
                      </Button>
                    </div>
                  )}
                </div>
                {change && isUsed ? (
                  <p className="transcript-text">
                    {diffWords(change.original, change.text).map((run, i) =>
                      run.type === "same" ? (
                        <span key={i}>{run.text} </span>
                      ) : (
                        // The colours are visual only; screen readers hear
                        // which words went and which came.
                        <Fragment key={i}>
                          {run.type === "removed" ? (
                            <del className="cleanup-word--removed">
                              <span className="sr-only">
                                {t("cleanup.removedWords")}{" "}
                              </span>
                              {run.text}
                            </del>
                          ) : (
                            <ins className="cleanup-word--added">
                              <span className="sr-only">
                                {t("cleanup.addedWords")}{" "}
                              </span>
                              {run.text}
                            </ins>
                          )}{" "}
                        </Fragment>
                      ),
                    )}
                  </p>
                ) : (
                  <p className="transcript-text">{segment.text}</p>
                )}
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
