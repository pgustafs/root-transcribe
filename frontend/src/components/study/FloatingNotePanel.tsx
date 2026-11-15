import { type ReactNode, useRef } from "react";
import { useTranslation } from "react-i18next";

import {
  type NoteCaptureActions,
  NoteCaptureControls,
} from "@/components/study/NoteCaptureControls";
import type { StudyStatus } from "@/lib/study/noteController";
import { FLOAT_SIZE } from "@/lib/study/studyChoice";
import { formatMsClock } from "@/utils/formatDuration";

type Props = {
  status: StudyStatus;
  /** The recording is running (not paused, not uploading). */
  recording: boolean;
  paused: boolean;
  durationMs: number;
  actions: NoteCaptureActions;
  /** The answer card of a marked question, when there is one. */
  answer?: ReactNode;
};

const FLOAT_HEIGHT_WITH_ANSWER = 520;

/**
 * The content of the floating window: the capture controls and one line that
 * says what is going on. Nothing else, so it stays small above a video.
 */
export function FloatingNotePanel({
  status,
  recording,
  paused,
  durationMs,
  actions,
  answer,
}: Props) {
  const { t } = useTranslation("study");
  const root = useRef<HTMLDivElement>(null);
  /** Make room for the answer. A window may only be resized from a click,
   * so this happens on the Question press, before there is an answer. */
  const resize = (height: number) => {
    try {
      root.current?.ownerDocument.defaultView?.resizeTo(
        FLOAT_SIZE.width,
        height,
      );
    } catch {
      // Not allowed here: the answer scrolls inside the window instead.
    }
  };
  const windowActions: NoteCaptureActions = {
    ...actions,
    onQuestion: (thought) => {
      resize(FLOAT_HEIGHT_WITH_ANSWER);
      actions.onQuestion(thought);
    },
  };
  const noteOpen = status.openSinceMs !== null;
  const canCapture = status.videoAvailable && recording;

  let line: string;
  if (noteOpen)
    line = t("dock.capturing", {
      time: formatMsClock(Math.max(0, durationMs - (status.openSinceMs ?? 0))),
    });
  else if (!status.videoAvailable) line = t("float.videoLost");
  else if (paused) line = t("dock.pausedHint");
  else if (!recording) line = t("float.notRecording");
  else if (status.offer) line = t("offer.line");
  else if (status.notice && status.notice !== "paused")
    line = t(`dock.notice.${status.notice}`, { defaultValue: status.notice });
  else if (!status.audioAvailable) line = t("dock.screenshotOnly");
  else
    line =
      status.count > 0
        ? t("page.notes", { count: status.count })
        : t("float.ready");

  return (
    <div
      ref={root}
      className="flex min-h-dvh flex-col gap-2 overflow-y-auto bg-background p-3 text-foreground"
      data-testid="study-float"
    >
      <NoteCaptureControls
        status={status}
        canCapture={canCapture}
        actions={windowActions}
        variant="floating"
        describedBy="study-float-state"
        focusOnMount
      />
      {/* Not a live region: the timer ticks every second. */}
      <p
        id="study-float-state"
        className="truncate text-center text-sm text-muted-foreground"
        title={line}
      >
        {line}
      </p>
      <span className="sr-only" aria-live="polite">
        {noteOpen
          ? t("dock.started")
          : status.savedTick > 0
            ? t("dock.saved")
            : ""}
      </span>
      {answer}
    </div>
  );
}
