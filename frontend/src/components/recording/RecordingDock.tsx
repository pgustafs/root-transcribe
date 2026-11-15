import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Progress } from "@/components/ui/progress";

import { useTranslation } from "react-i18next";

import { Alert } from "@/components/Alert";
import type { UploadProgress } from "@/lib/uploadManager";
import { useShortcutHint } from "@/hooks/useStudy";
import type { StudyStatus } from "@/lib/study/noteController";
import {
  formatDuration as formatSeconds,
  formatMsClock,
} from "@/utils/formatDuration";

/** Study notes of the running recording, and what the dock can do with them. */
export type StudyDockProps = {
  status: StudyStatus;
  onToggle: () => void;
  onViewNotes: () => void;
  onChangeSource: () => void;
  onSetup: () => void;
};

export type RecorderDockStatus = "recording" | "paused" | "uploading" | "retry";

type Props = {
  status: RecorderDockStatus;
  title: string;
  durationMs: number;
  /** Input level 0..1; the meter renders whenever the recorder is live. */
  level: number;
  showLevel: boolean;
  trackWarning: string | null;
  noTabAudio: boolean;
  uploadProgress: UploadProgress | null;
  uploadError: string | null;
  /** The durable local copy has stopped updating mid-capture: the user must be
   * told, or a crash loses the meeting silently (issue #37 review r20). */
  storageDegraded: boolean;
  onStop: () => void;
  onRetry: () => void;
  onPause: () => void;
  onResume: () => void;
  /** Opens the destructive-cancel confirmation — never cancels directly. */
  onCancelRequest: () => void;
  busy: boolean;
  /** Present while study notes are being taken from the shared tab. */
  study?: StudyDockProps;
};

/** Status is a Label with TEXT, never color alone; the pulse dot is decorative
 * (and stops pulsing under prefers-reduced-motion via CSS). */
const STATUS_PRESENTATION: Record<
  RecorderDockStatus,
  { color: "red" | "grey" | "blue" | "orange"; key: string; pulse: boolean }
> = {
  recording: { color: "red", key: "recordingBar.statusRecording", pulse: true },
  paused: { color: "grey", key: "recordingBar.statusPaused", pulse: false },
  uploading: {
    color: "blue",
    key: "recordingBar.statusUploading",
    pulse: false,
  },
  retry: { color: "orange", key: "recordingBar.statusRetry", pulse: false },
};

export function RecordingDock({
  status,
  title,
  durationMs,
  level,
  showLevel,
  trackWarning,
  noTabAudio,
  uploadProgress,
  uploadError,
  storageDegraded,
  onStop,
  onRetry,
  onPause,
  onResume,
  onCancelRequest,
  busy,
  study,
}: Props) {
  const { t } = useTranslation(["meetings", "common", "study"]);
  const presentation = STATUS_PRESENTATION[status];
  const shortcutHint = useShortcutHint();
  const notes = study?.status;
  const noteOpen = notes ? notes.openSinceMs !== null : false;
  // New captures need a live shared tab and a running recording; an open
  // note can always be stopped.
  const canCapture =
    Boolean(notes?.videoAvailable) && status === "recording" && !busy;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <div>
            <Badge variant="secondary">{t(presentation.key)}</Badge>
          </div>
          <div className="recording-dock-title-cell">
            <p className={cn("app-copy", "recording-dock-title")}>
              {/* NOT a live region: a per-second timer would be announced on
                  every tick. State changes are announced by the status Label
                  swap and the warnings region below. */}
              <strong>{title}</strong>{" "}
              <span>{formatSeconds(durationMs / 1000)}</span>
            </p>
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <div>
                  {status === "retry" ? (
                    <Button
                      onClick={onRetry}
                      disabled={busy}
                      aria-busy={busy}
                      variant="default"
                      size="sm"
                    >
                      {busy && (
                        <Spinner
                          data-icon="inline-start"
                          aria-label={undefined}
                        />
                      )}
                      {t("recordingBar.retryUpload")}
                    </Button>
                  ) : (
                    <Button
                      onClick={onStop}
                      disabled={busy}
                      aria-busy={busy}
                      // One turquoise action per surface: while notes are
                      // taken, that is Take note.
                      variant={study ? "outline" : "default"}
                      size="sm"
                    >
                      {busy && (
                        <Spinner
                          data-icon="inline-start"
                          aria-label={undefined}
                        />
                      )}
                      {t("recordModal.stop")}
                    </Button>
                  )}
                </div>
                {status === "recording" && (
                  <div>
                    <Button onClick={onPause} variant="outline" size="sm">
                      {t("recordModal.pause")}
                    </Button>
                  </div>
                )}
                {status === "paused" && (
                  <div>
                    <Button onClick={onResume} variant="outline" size="sm">
                      {t("recordModal.resume")}
                    </Button>
                  </div>
                )}
                <div>
                  {/* Cancel stays ENABLED while uploading — cancelling an
                      in-flight upload is a supported, confirmed flow. */}
                  <Button
                    onClick={onCancelRequest}
                    variant="destructive"
                    size="sm"
                  >
                    {t("recordModal.cancel")}
                  </Button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      {study && notes && (
        <div
          className="flex flex-wrap items-center gap-x-3 gap-y-2"
          data-testid="study-dock"
        >
          <Button
            onClick={study.onToggle}
            disabled={!noteOpen && !canCapture}
            variant="default"
            size="sm"
            aria-describedby="study-dock-state"
          >
            {noteOpen ? t("study:dock.stopNote") : t("study:dock.takeNote")}
          </Button>
          {shortcutHint && (
            <kbd className="rounded-md border bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
              {shortcutHint}
            </kbd>
          )}
          {/* Not a live region: the timer ticks every second. Start and save
              are announced once in the region below. */}
          <span id="study-dock-state" className="text-sm text-muted-foreground">
            {noteOpen
              ? t("study:dock.capturing", {
                  time: formatMsClock(
                    Math.max(0, durationMs - (notes.openSinceMs ?? 0)),
                  ),
                })
              : status === "paused"
                ? t("study:dock.pausedHint")
                : notes.notebookTitle}
          </span>
          <Button onClick={study.onViewNotes} variant="outline" size="sm">
            {notes.count > 0
              ? t("study:dock.viewNotes", { count: notes.count })
              : t("study:dock.viewNotesNone")}
          </Button>
          <Button onClick={study.onChangeSource} variant="ghost" size="sm">
            {t("study:dock.changeSource")}
          </Button>
          <Button onClick={study.onSetup} variant="ghost" size="sm">
            {t("study:dock.setupShortcut")}
          </Button>
          <span className="sr-only" aria-live="polite">
            {noteOpen
              ? t("study:dock.started")
              : notes.savedTick > 0
                ? `${t("study:dock.saved")} ${t("study:dock.viewNotes", { count: notes.count })}`
                : ""}
          </span>
        </div>
      )}
      {showLevel && (
        <div>
          <Progress
            value={Math.round(level * 100)}
            aria-label={t("recordingBar.level")}
          />
        </div>
      )}
      {uploadProgress && uploadProgress.partsTotal > 1 && (
        <div>
          <Progress
            value={Math.round(
              (uploadProgress.partsDone / uploadProgress.partsTotal) * 100,
            )}
            aria-label={t("recordingBar.uploading")}
          />
        </div>
      )}
      {/* Warnings appear while the user is working elsewhere on the page, so
          they live in a polite live region rather than relying on sight. */}
      <div>
        <div aria-live="polite">
          <div className="flex flex-col gap-4">
            {trackWarning && (
              <div>
                <Alert
                  variant="warning"
                  isInline={true}
                  title={
                    trackWarning === "tab-ended"
                      ? t("recordingBar.tabTrackEnded")
                      : t("recordingBar.micTrackEnded")
                  }
                />
              </div>
            )}
            {noTabAudio && (
              <div>
                <Alert
                  variant="warning"
                  isInline={true}
                  title={t("recordingBar.noTabAudio")}
                />
              </div>
            )}
            {notes && !notes.audioAvailable && notes.videoAvailable && (
              <div>
                <Alert
                  variant="warning"
                  isInline={true}
                  title={t("study:dock.screenshotOnly")}
                >
                  {t("study:dock.screenshotOnlyHelp")}
                </Alert>
              </div>
            )}
            {notes && !notes.videoAvailable && (
              <div>
                <Alert
                  variant="warning"
                  isInline={true}
                  title={t("study:dock.videoLost")}
                />
              </div>
            )}
            {notes?.notice &&
              notes.notice !== "sharing_lost" &&
              notes.notice !== "paused" && (
                <div>
                  <Alert
                    variant="info"
                    isInline={true}
                    title={t(`study:dock.notice.${notes.notice}`, {
                      defaultValue: notes.notice,
                    })}
                  />
                </div>
              )}
            {notes?.notice === "paused" && (
              <div>
                <Alert
                  variant="info"
                  isInline={true}
                  title={t("study:dock.notice.paused")}
                />
              </div>
            )}
            {storageDegraded && (
              <div>
                <Alert
                  variant="danger"
                  isInline={true}
                  title={t("recordingBar.storageDegraded")}
                />
              </div>
            )}
            {uploadError && (
              <div>
                <Alert variant="danger" isInline={true} title={uploadError} />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
