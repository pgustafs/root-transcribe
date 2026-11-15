/**
 * "Change source": the shared tab moved on to another video (the recorder
 * cannot see that), so the learner says where the next notes belong and shares
 * the tab again. Starts a new interval: nothing from the old source leaks in.
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Alert } from "@/components/Alert";
import {
  type StudyTarget,
  useRecordingContext,
} from "@/components/recording/RecordingProvider";
import { StudyCropStep } from "@/components/study/StudyCropStep";
import { StudySourceFields } from "@/components/study/StudySourceFields";
import {
  EMPTY_STUDY_CHOICE,
  resolveNotebook,
  sourceUrlProblem,
  type StudyChoice,
} from "@/lib/study/studyChoice";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FieldGroup } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { useNotebooks } from "@/hooks/useStudy";
import type { StudyMode } from "@/lib/study/noteStore";

type Props = {
  workspaceId: string;
  mode: StudyMode;
  onClose: () => void;
  onChange: (next: StudyTarget) => Promise<boolean>;
};

export function ChangeSourceDialog({
  workspaceId,
  mode,
  onClose,
  onChange,
}: Props) {
  const { t } = useTranslation(["study", "common"]);
  const recording = useRecordingContext();
  const notebooks = useNotebooks(workspaceId);
  const [choice, setChoice] = useState<StudyChoice>({
    ...EMPTY_STUDY_CHOICE,
    mode,
  });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [cropping, setCropping] = useState(false);

  const share = async () => {
    if (busy || sourceUrlProblem(choice)) return;
    setBusy(true);
    setProblem(null);
    try {
      const notebook = await resolveNotebook(
        workspaceId,
        choice,
        notebooks.data ?? [],
        t("study:page.defaultNotebook"),
      );
      const changed = await onChange({
        notebookId: notebook.id,
        notebookTitle: notebook.title,
        mode: choice.mode,
      });
      if (changed) setCropping(true);
      else setProblem(t("study:changeSource.notShared"));
    } catch {
      setProblem(t("study:setup.notebookFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>
            {cropping ? t("study:crop.title") : t("study:changeSource.title")}
          </DialogTitle>
        </DialogHeader>
        {cropping ? (
          <StudyCropStep
            getPreview={recording.studyPreview}
            initial={recording.studyCrop()}
            onChange={(crop) => void recording.setStudyCrop(crop)}
          />
        ) : (
          <FieldGroup>
            <p className="text-sm text-muted-foreground">
              {t("study:changeSource.intro")}
            </p>
            {problem && (
              <Alert variant="warning" isInline isLiveRegion title={problem} />
            )}
            <StudySourceFields
              idPrefix="change-source"
              choice={choice}
              onChange={setChoice}
              notebooks={notebooks.data ?? []}
              suggestedTitle={t("study:page.defaultNotebook")}
            />
          </FieldGroup>
        )}
        <DialogFooter>
          {cropping ? (
            <Button onClick={onClose}>{t("study:crop.done")}</Button>
          ) : (
            <>
              <Button
                onClick={() => void share()}
                disabled={busy || sourceUrlProblem(choice)}
                aria-busy={busy}
              >
                {busy && (
                  <Spinner data-icon="inline-start" aria-hidden="true" />
                )}
                {t("study:changeSource.share")}
              </Button>
              <Button variant="link" onClick={onClose}>
                {t("common:actions.cancel")}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
