import type { RecordingLanguage } from "@/api/types";
import { RecordingLanguageField } from "@/components/recording/RecordingLanguageField";
import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Field,
  FieldLabel,
  FieldDescription,
  FieldGroup,
} from "@/components/ui/field";
import {
  NativeSelectOption,
  NativeSelect,
} from "@/components/ui/native-select";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { AppConfig } from "@/api/system";
import { getConfig } from "@/api/system";
import { Alert } from "@/components/Alert";
import { useRecordingContext } from "@/components/recording/RecordingProvider";
import { useConfig } from "@/hooks/useConfig";
import { listAudioInputs } from "@/hooks/useMediaRecorder";

/** How long the readiness probe may take before we stop waiting on it. The
 * probe is an optimisation — the upload path is still gated server-side and
 * the local copy is preserved — so a slow or unreachable /config must never
 * stop someone from recording (issue #37). */
const READINESS_PROBE_TIMEOUT_MS = 4000;

/** Ask the server whether transcription is up, time-boxed, falling back to the
 * cached answer on any failure. Never throws, never hangs. */
async function probeAvailability(
  cached: AppConfig["meetings_status"] | undefined,
): Promise<AppConfig["meetings_status"] | undefined> {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), READINESS_PROBE_TIMEOUT_MS);
  try {
    return (await getConfig(abort.signal)).meetings_status;
  } catch {
    return cached;
  } finally {
    clearTimeout(timer);
  }
}

type Props = {
  workspaceId: string;
  isOpen: boolean;
  onClose: () => void;
  /** Called with the recording id after the recording is stopped + uploaded. */
  onUploaded?: (recordingId: string) => void;
};

export function RecordMeetingModal({
  workspaceId,
  isOpen,
  onClose,
  onUploaded,
}: Props) {
  const { t } = useTranslation(["meetings", "common"]);
  const recording = useRecordingContext();
  const config = useConfig();

  const [title, setTitle] = useState("");
  const [requestedLanguage, setRequestedLanguage] =
    useState<RecordingLanguage>("auto");
  const [includeTab, setIncludeTab] = useState(false);
  const [consent, setConsent] = useState(false);
  const [starting, setStarting] = useState(false);
  const [serviceDown, setServiceDown] = useState(false);
  const [devices, setDevices] = useState<{ deviceId: string; label: string }[]>(
    [],
  );
  const [deviceId, setDeviceId] = useState("");

  useEffect(() => {
    if (!isOpen) return;
    // Labels are empty until mic permission has been granted once; generic
    // names are shown for those.
    void listAudioInputs().then(setDevices);
  }, [isOpen]);

  // Server policy (issue #6): when the deployment requires the attestation,
  // Start stays disabled until it is given — the checkbox gated nothing before.
  const consentRequired = config.data?.meeting_consent_required ?? false;
  const consentBlocked = consentRequired && !consent;

  const labelId = "record-meeting-title";

  const handleStart = async () => {
    if (starting || consentBlocked || !recording.isSupported) return;
    setStarting(true);
    setServiceDown(false);
    try {
      // Readiness preflight (issue #37, after #36): don't START a recording the
      // deployment cannot transcribe — the user would only find out at upload.
      //
      // Deliberately NOT `config.refetch()`. React Query defaults to
      // `networkMode: "online"`, so a query it considers offline is PAUSED and
      // its refetch promise never settles — and a refetch carries no timeout
      // either. Either one leaves this button spinning forever with no error,
      // which is exactly what it did on a live pod. A direct, time-boxed
      // request cannot do that.
      const availability = await probeAvailability(
        config.data?.meetings_status,
      );
      if (availability && availability !== "available") {
        setServiceDown(true);
        return;
      }
      const ok = await recording.start({
        workspaceId,
        title: title.trim() || t("recordModal.title"),
        includeTabAudio: includeTab,
        consent,
        requestedLanguage,
        deviceId: deviceId || undefined,
        onUploaded,
      });
      // Only close once the recording is actually live; a mic-denied /
      // insecure-context failure keeps the dialog open with the error shown.
      if (ok) {
        setTitle("");
        setIncludeTab(false);
        setConsent(false);
        onClose();
      }
    } finally {
      // ALWAYS clears: a spinner that outlives its work tells the user nothing
      // and offers no way out.
      setStarting(false);
    }
  };

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        aria-labelledby={labelId}
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle id={labelId}>{t("recordModal.title")}</DialogTitle>
        </DialogHeader>
        <div className="min-w-0">
          <form
            id="record-meeting-form"
            onSubmit={(event) => {
              event.preventDefault();
              void handleStart();
            }}
          >
            <FieldGroup>
              {serviceDown && (
                <Alert
                  variant="danger"
                  isInline={true}
                  isLiveRegion={true}
                  title={t("recordModal.serviceUnavailable")}
                />
              )}
              <Field>
                <FieldLabel htmlFor="meeting-title">
                  {t("recordModal.name")}
                </FieldLabel>
                <Input
                  id="meeting-title"
                  value={title}
                  placeholder={t("recordModal.namePlaceholder")}
                  onChange={(_e) => {
                    const v = _e.currentTarget.value;
                    return setTitle(v);
                  }}
                />
              </Field>
              <RecordingLanguageField
                value={requestedLanguage}
                onChange={setRequestedLanguage}
              />
              {devices.length > 0 && (
                <Field>
                  <FieldLabel htmlFor="recording-device">
                    {t("recordModal.device")}
                  </FieldLabel>
                  <NativeSelect
                    id="recording-device"
                    value={deviceId}
                    onChange={(_e) => {
                      const v = _e.currentTarget.value;
                      return setDeviceId(v);
                    }}
                  >
                    <NativeSelectOption value="">
                      {t("recordModal.deviceDefault")}
                    </NativeSelectOption>
                    {devices.map((d, i) => (
                      <NativeSelectOption
                        key={d.deviceId || i}
                        value={d.deviceId}
                      >
                        {d.label ||
                          t("recordModal.deviceGeneric", { n: i + 1 })}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </Field>
              )}
              <Field orientation="horizontal">
                <Checkbox
                  id="include-tab-audio"
                  checked={includeTab}
                  onCheckedChange={(state) => {
                    const v = state === true;
                    return setIncludeTab(v);
                  }}
                />
                <FieldLabel htmlFor="include-tab-audio">
                  {t("recordModal.includeTab")}
                </FieldLabel>
              </Field>
              <Field>
                <Field orientation="horizontal">
                  <Checkbox
                    id="participant-consent"
                    aria-describedby={
                      consentBlocked ? "participant-consent-help" : undefined
                    }
                    checked={consent}
                    onCheckedChange={(state) => {
                      const v = state === true;
                      return setConsent(v);
                    }}
                  />
                  <FieldLabel htmlFor="participant-consent">
                    {t("recordModal.consent")}
                  </FieldLabel>
                </Field>
                {consentBlocked && (
                  <div>
                    <div>
                      <FieldDescription id="participant-consent-help">
                        {t("recordModal.consentRequired")}
                      </FieldDescription>
                    </div>
                  </div>
                )}
              </Field>
              {!recording.isSupported && (
                <Alert
                  variant="warning"
                  isInline={true}
                  title={t("recordModal.needsSecureContext")}
                />
              )}
              {recording.startError && (
                // Already a complete, actionable sentence — and NOT a microphone
                // problem, so it must not wear the mic-error title.
                <Alert
                  variant="danger"
                  isInline={true}
                  isLiveRegion={true}
                  title={recording.startError}
                />
              )}
              {recording.error && recording.error !== "insecure-context" && (
                <Alert
                  variant="danger"
                  isInline={true}
                  isLiveRegion={true}
                  title={t("recordModal.micError")}
                >
                  {recording.error}
                </Alert>
              )}
            </FieldGroup>
          </form>
        </div>
        <DialogFooter>
          <Button
            type="submit"
            form="record-meeting-form"
            disabled={!recording.isSupported || starting || consentBlocked}
            aria-busy={starting}
            variant="default"
          >
            {starting && (
              <Spinner data-icon="inline-start" aria-label={undefined} />
            )}
            {t("recordModal.start")}
          </Button>
          <Button onClick={onClose} variant="link">
            {t("common:actions.cancel")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
