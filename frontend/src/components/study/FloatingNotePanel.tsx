import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import type { StudyStatus } from "@/lib/study/noteController";
import { formatMsClock } from "@/utils/formatDuration";

type Props = {
  status: StudyStatus;
  /** The recording is running (not paused, not uploading). */
  recording: boolean;
  paused: boolean;
  durationMs: number;
  onToggle: () => void;
};

/**
 * The content of the floating window: Take note / Stop note, and one line
 * that says what is going on. Nothing else, so it stays small above a video.
 */
export function FloatingNotePanel({
  status,
  recording,
  paused,
  durationMs,
  onToggle,
}: Props) {
  const { t } = useTranslation("study");
  const button = useRef<HTMLButtonElement>(null);
  const noteOpen = status.openSinceMs !== null;
  const canCapture = status.videoAvailable && recording;

  // Space or Enter works as soon as the window has the focus.
  useEffect(() => {
    button.current?.focus();
  }, []);

  let line: string;
  if (noteOpen)
    line = t("dock.capturing", {
      time: formatMsClock(Math.max(0, durationMs - (status.openSinceMs ?? 0))),
    });
  else if (!status.videoAvailable) line = t("float.videoLost");
  else if (paused) line = t("dock.pausedHint");
  else if (!recording) line = t("float.notRecording");
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
      className="flex min-h-dvh flex-col justify-center gap-2 bg-background p-3 text-foreground"
      data-testid="study-float"
    >
      <Button
        ref={button}
        onClick={onToggle}
        disabled={!noteOpen && !canCapture}
        variant="default"
        size="lg"
        className="w-full"
        aria-describedby="study-float-state"
      >
        {noteOpen ? t("dock.stopNote") : t("dock.takeNote")}
      </Button>
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
    </div>
  );
}
