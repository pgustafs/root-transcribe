import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Progress } from "@/components/ui/progress";

import { useTranslation } from "react-i18next";

import { Alert } from "@/components/Alert";
import type { UploadProgress } from "@/lib/uploadManager";
import { formatDuration as formatSeconds } from "@/utils/formatDuration";

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
}: Props) {
  const { t } = useTranslation(["meetings", "common"]);
  const presentation = STATUS_PRESENTATION[status];

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
                      variant="default"
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
