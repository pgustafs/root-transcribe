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
import { StudyCropStep } from "@/components/study/StudyCropStep";
import { StudySourceFields } from "@/components/study/StudySourceFields";
import {
  EMPTY_STUDY_CHOICE,
  resolveNotebook,
  sourceUrlProblem,
  type StudyChoice,
} from "@/lib/study/studyChoice";
import { Switch } from "@/components/ui/switch";
import { useNotebooks } from "@/hooks/useStudy";

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
  /** The caller may take study notes here (server-computed capability). */
  canStudy?: boolean;
};

export function RecordMeetingModal({
  workspaceId,
  isOpen,
  onClose,
  onUploaded,
  canStudy = false,
}: Props) {
  const { t } = useTranslation(["meetings", "common", "study"]);
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
  // Study notes: offered once tab sharing is chosen.
  const [studyOn, setStudyOn] = useState(false);
  const [studyChoice, setStudyChoice] =
    useState<StudyChoice>(EMPTY_STUDY_CHOICE);
  const [studyProblem, setStudyProblem] = useState(false);
  // After sharing starts: choose the video area before the dialog closes.
  const [cropping, setCropping] = useState(false);
  const takeStudyNotes = canStudy && includeTab && studyOn;
  const notebooks = useNotebooks(workspaceId, isOpen && canStudy && includeTab);
  const studyBlocked = takeStudyNotes && sourceUrlProblem(studyChoice);

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
    if (starting || consentBlocked || studyBlocked || !recording.isSupported)
      return;
    setStarting(true);
    setServiceDown(false);
    setStudyProblem(false);
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
      const recordingTitle = title.trim() || t("recordModal.title");
      let study;
      if (takeStudyNotes) {
        try {
          const notebook = await resolveNotebook(
            workspaceId,
            studyChoice,
            notebooks.data ?? [],
            recordingTitle,
          );
          study = {
            notebookId: notebook.id,
            notebookTitle: notebook.title,
            mode: studyChoice.mode,
          };
        } catch {
          setStudyProblem(true);
          return;
        }
      }
      const ok = await recording.start({
        workspaceId,
        study,
        title: recordingTitle,
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
        setStudyOn(false);
        setStudyChoice(EMPTY_STUDY_CHOICE);
        // Recording is live. With study notes, the shared tab is shown once
        // so the video area can be chosen; the dialog closes on Done.
        if (study) setCropping(true);
        else onClose();
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
          <DialogTitle id={labelId}>
            {cropping ? t("study:crop.title") : t("recordModal.title")}
          </DialogTitle>
        </DialogHeader>
        {cropping && (
          <StudyCropStep
            getPreview={recording.studyPreview}
            initial={recording.studyCrop()}
            onChange={(crop) => void recording.setStudyCrop(crop)}
          />
        )}
        <div className={cropping ? "hidden" : "min-w-0"}>
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
              {canStudy && includeTab && (
                <Field>
                  <Field orientation="horizontal">
                    <Switch
                      id="take-study-notes"
                      checked={studyOn}
                      onCheckedChange={setStudyOn}
                      aria-describedby="take-study-notes-help"
                    />
                    <FieldLabel htmlFor="take-study-notes">
                      {t("study:setup.enable")}
                    </FieldLabel>
                  </Field>
                  <FieldDescription id="take-study-notes-help">
                    {t("study:setup.enableHelp")}
                  </FieldDescription>
                </Field>
              )}
              {takeStudyNotes && (
                <StudySourceFields
                  idPrefix="study"
                  choice={studyChoice}
                  onChange={setStudyChoice}
                  notebooks={notebooks.data ?? []}
                  suggestedTitle={title.trim() || t("recordModal.title")}
                />
              )}
              {studyProblem && (
                <Alert
                  variant="danger"
                  isInline={true}
                  isLiveRegion={true}
                  title={t("study:setup.notebookFailed")}
                />
              )}
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
          {cropping ? (
            <Button
              onClick={() => {
                // Still inside the click: the browser allows the floating
                // Take note window to open now.
                recording.openStudyFloat();
                onClose();
              }}
              variant="default"
            >
              {t("study:crop.done")}
            </Button>
          ) : (
            <>
              <Button
                type="submit"
                form="record-meeting-form"
                disabled={
                  !recording.isSupported ||
                  starting ||
                  consentBlocked ||
                  studyBlocked
                }
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
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
