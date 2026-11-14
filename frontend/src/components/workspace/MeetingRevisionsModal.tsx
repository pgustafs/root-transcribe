import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { HistoryIcon } from "lucide-react";
import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { FieldDescription, Field, FieldLabel } from "@/components/ui/field";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { MeetingSegment } from "@/api/types";
import { Alert } from "@/components/Alert";
import { DataState } from "@/components/DataState";
import { useToast } from "@/components/toast/useToast";
import { revisionActionErrorMessage } from "@/lib/meetingErrors";
import { TranscriptDiff } from "@/components/workspace/TranscriptDiff";
import {
  useApproveRevision,
  useDiscardRevision,
  useRevisions,
  useRevisionSegments,
} from "@/hooks/useMeetings";

function transcriptText(segments: MeetingSegment[] | undefined): string {
  return (segments ?? [])
    .map((s) => `${s.speaker_name ?? s.speaker_label}: ${s.text}`)
    .join("\n");
}

export function MeetingRevisionsModal({
  workspaceId,
  recordingId,
  displayRevisionId,
  canEdit,
  onClose,
}: {
  workspaceId: string;
  recordingId: string;
  /** The revision the transcript view currently shows (approved, else newest). */
  displayRevisionId: string | null;
  canEdit: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation(["meetings", "common"]);
  const revisions = useRevisions(workspaceId, recordingId);
  const approve = useApproveRevision(workspaceId, recordingId);
  const discard = useDiscardRevision(workspaceId, recordingId);
  // EITHER decision in flight blocks BOTH controls: they are two writes to the
  // same recording, and letting a user fire Discard while an Approve is still
  // running is the client-side half of the race the row lock closes server-side
  // (issue #39 review round 1).
  const deciding = approve.isPending || discard.isPending;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const toast = useToast();

  const rows = useMemo(
    () => (revisions.data ?? []).filter((r) => r.state !== "discarded"),
    [revisions.data],
  );
  // Default selection: the newest revision that is NOT what is already shown —
  // the thing awaiting a decision.
  const selected =
    rows.find((r) => r.id === selectedId) ??
    rows.find((r) => r.id !== displayRevisionId) ??
    rows[0];

  const baseSegments = useRevisionSegments(
    workspaceId,
    recordingId,
    displayRevisionId,
  );
  const candidateSegments = useRevisionSegments(
    workspaceId,
    recordingId,
    selected?.id ?? null,
  );
  const comparing = selected !== undefined && selected.id !== displayRevisionId;

  const labelId = "meeting-revisions-title";
  return (
    <Dialog
      open={true}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        aria-labelledby={labelId}
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-5xl"
      >
        <DialogHeader>
          <DialogTitle id={labelId}>{t("detail.revisionsTitle")}</DialogTitle>
        </DialogHeader>
        <div className="min-w-0">
          <DataState
            isLoading={revisions.isLoading}
            error={revisions.error}
            isEmpty={rows.length === 0}
            emptyTitle={t("detail.revisionsEmpty")}
            emptyIcon={HistoryIcon}
          >
            <div className="flex flex-col gap-4">
              <ToggleGroup
                type="single"
                variant="outline"
                spacing={2}
                className="flex-wrap"
                aria-label={t("detail.revisionsTitle")}
                value={selected?.id}
                onValueChange={(id) => {
                  if (id) setSelectedId(id);
                }}
              >
                {rows.map((rev) => (
                  <div key={rev.id}>
                    <ToggleGroupItem value={rev.id}>
                      {t("detail.revisionItem", { n: rev.revision_number })}
                      {" · "}
                      {t(`detail.revisionState.${rev.state}`)}
                    </ToggleGroupItem>
                  </div>
                ))}
              </ToggleGroup>
              {selected && selected.conflict_count > 0 && (
                <Alert
                  variant="warning"
                  isInline={true}
                  title={t("detail.revisionConflicts", {
                    count: selected.conflict_count,
                  })}
                />
              )}
              {selected && selected.carried_edits > 0 && (
                <Alert
                  variant="info"
                  isInline={true}
                  title={t("detail.revisionCarried", {
                    count: selected.carried_edits,
                  })}
                />
              )}
              {comparing && (
                <DataState
                  isLoading={
                    baseSegments.isLoading || candidateSegments.isLoading
                  }
                  error={baseSegments.error ?? candidateSegments.error}
                  isEmpty={false}
                >
                  <TranscriptDiff
                    before={transcriptText(baseSegments.data)}
                    after={transcriptText(candidateSegments.data)}
                  />
                </DataState>
              )}
              {/* A FIRST transcript has nothing to diff against, and approving it is
              the whole point of opening this (issue #39) — so show what is being
              approved instead of an empty body between the labels and the
              footer. */}
              {canEdit && selected && selected.state !== "approved" && (
                <Field>
                  <FieldLabel htmlFor="meeting-approval-note">
                    {t("detail.approvalNote")}
                  </FieldLabel>
                  <Input
                    id="meeting-approval-note"
                    value={note}
                    aria-describedby="meeting-approval-note-help"
                    onChange={(_e) => {
                      const v = _e.currentTarget.value;
                      return setNote(v);
                    }}
                  />
                  <div>
                    <div>
                      <FieldDescription id="meeting-approval-note-help">
                        {t("detail.approvalNoteHelp")}
                      </FieldDescription>
                    </div>
                  </div>
                </Field>
              )}
              {!comparing && selected && (
                <DataState
                  isLoading={candidateSegments.isLoading}
                  error={candidateSegments.error}
                  isEmpty={(candidateSegments.data?.length ?? 0) === 0}
                  emptyTitle={t("detail.revisionsEmpty")}
                >
                  <pre className={cn("app-copy", "meeting-revision-text")}>
                    {transcriptText(candidateSegments.data)}
                  </pre>
                </DataState>
              )}
              {/* Approve/discard used to fail with nothing rendered at all: a 403, a
              409 or an offline click left the modal exactly as it was. */}
              <div aria-live="polite">
                {(approve.isError || discard.isError) && (
                  <Alert
                    variant="danger"
                    isInline={true}
                    title={revisionActionErrorMessage(
                      approve.error ?? discard.error,
                      t,
                    )}
                  />
                )}
              </div>
            </div>
          </DataState>
        </div>
        <DialogFooter>
          {canEdit && selected && selected.state !== "approved" && (
            <>
              <Button
                onClick={() =>
                  approve.mutate(
                    { revisionId: selected.id, note: note || undefined },
                    {
                      onSuccess: () => {
                        toast.success(
                          t("detail.toastApproved", {
                            n: selected.revision_number,
                          }),
                        );
                        onClose();
                      },
                    },
                  )
                }
                disabled={deciding || approve.isPending}
                aria-busy={approve.isPending}
                variant="default"
              >
                {approve.isPending && (
                  <Spinner
                    data-icon="inline-start"
                    aria-label={t("detail.approvingRevision")}
                  />
                )}
                {t("detail.approveRevision")}
              </Button>
              {/* Never offer to throw away the LAST surviving revision: that
                leaves a completed recording with no displayable transcript and
                no way back (issue #39). The API refuses it too — this just
                keeps the button from being there to click. */}
              {rows.length > 1 && (
                <Button
                  onClick={() =>
                    discard.mutate(selected.id, {
                      onSuccess: () => {
                        toast.success(
                          t("detail.toastDiscarded", {
                            n: selected.revision_number,
                          }),
                        );
                        onClose();
                      },
                    })
                  }
                  disabled={deciding}
                  variant="destructive"
                >
                  {t("detail.discardRevision")}
                </Button>
              )}
            </>
          )}
          <Button onClick={onClose} variant="link">
            {t("common:actions.close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
