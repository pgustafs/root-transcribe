import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";
import { Checkbox } from "@/components/ui/checkbox";
import { Field, FieldLabel } from "@/components/ui/field";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import { Alert } from "@/components/Alert";
import { useMediaRecorder } from "@/hooks/useMediaRecorder";
import { formatDuration } from "@/utils/formatDuration";

const fmt = (ms: number): string => formatDuration(ms / 1000);

type Props = {
  isOpen: boolean;
  title: string;
  requireConsent?: boolean;
  consentLabel?: string;
  onClose: () => void;
  onSubmit: (blob: Blob, filename: string, consent: boolean) => Promise<void>;
};

export function VoiceEnrollModal({
  isOpen,
  title,
  requireConsent = false,
  consentLabel,
  onClose,
  onSubmit,
}: Props) {
  const { t } = useTranslation(["meetings", "common"]);
  const recorder = useMediaRecorder();
  const [consent, setConsent] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const labelId = "voice-enroll-title";
  const recording = recorder.status !== "idle";

  const close = () => {
    recorder.cancel();
    setConsent(false);
    setError(null);
    onClose();
  };

  const stopAndSubmit = async () => {
    setError(null);
    const { blob, mimeType } = await recorder.stop();
    const ext = mimeType.includes("ogg")
      ? "ogg"
      : mimeType.includes("mp4")
        ? "mp4"
        : "webm";
    setSubmitting(true);
    try {
      await onSubmit(blob, `sample.${ext}`, consent);
      setSubmitting(false);
      setConsent(false);
      onClose();
    } catch (e) {
      setSubmitting(false);
      setError(getErrorMessage(e));
    }
  };

  const canStop = !requireConsent || consent;

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <DialogContent
        aria-labelledby={labelId}
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle id={labelId}>{title}</DialogTitle>
        </DialogHeader>
        <div className="min-w-0">
          <div className="flex flex-col gap-4">
            <div>
              <p className="app-copy">{t("speakers.recordSample")}</p>
            </div>
            {requireConsent && (
              <div>
                <Field orientation="horizontal">
                  <Checkbox
                    id="voice-consent"
                    checked={consent}
                    onCheckedChange={(state) => {
                      const v = state === true;
                      return setConsent(v);
                    }}
                  />
                  <FieldLabel htmlFor="voice-consent">
                    {consentLabel ?? t("speakers.consentMine")}
                  </FieldLabel>
                </Field>
              </div>
            )}
            {recording && (
              <div>
                <p role="status" className="app-copy">
                  {t("recordModal.recording")}— {fmt(recorder.durationMs)}
                </p>
              </div>
            )}
            {!recorder.isSupported && (
              <div>
                <Alert
                  variant="warning"
                  isInline={true}
                  title={t("recordModal.needsSecureContext")}
                />
              </div>
            )}
            {(recorder.error || error) &&
              (recorder.error === "insecure-context" ? null : (
                <div>
                  <Alert
                    variant="danger"
                    isInline={true}
                    isLiveRegion={true}
                    title={recorder.error ?? error ?? ""}
                  />
                </div>
              ))}
          </div>
        </div>
        <DialogFooter>
          {recorder.status === "idle" ? (
            <Button
              onClick={() => recorder.start({})}
              disabled={(requireConsent && !consent) || !recorder.isSupported}
              variant="default"
            >
              {t("recordModal.start")}
            </Button>
          ) : (
            <Button
              onClick={stopAndSubmit}
              disabled={submitting || !canStop}
              aria-busy={submitting}
              variant="default"
            >
              {submitting && (
                <Spinner data-icon="inline-start" aria-label={undefined} />
              )}
              {t("recordModal.stop")}
            </Button>
          )}
          <Button onClick={close} variant="link">
            {t("recordModal.cancel")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
