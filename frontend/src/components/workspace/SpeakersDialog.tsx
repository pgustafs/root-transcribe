import { type FormEvent, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { SparklesIcon } from "lucide-react";

import { getErrorMessage } from "@/api/client";
import type { MeetingSegment } from "@/api/types";
import { Alert } from "@/components/Alert";
import { useToast } from "@/components/toast/useToast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import {
  useAssignSpeakerByLabel,
  useDecideSpeakerName,
  useIdentifySpeakerNames,
  useSpeakerNameRun,
} from "@/hooks/useMeetings";
import { cn } from "@/lib/utils";
import { formatMsClock } from "@/utils/formatDuration";
import { speakerLabel } from "@/utils/speakerLabel";

const FORM_ID = "speakers-form";

/**
 * Name the voices in one recording: type a name per speaker, or let the
 * language model look for names in what was said ("my name is Peter"). The
 * model only suggests, with the words it relies on; a person uses or dismisses
 * each name. Names typed here belong to this recording only.
 */
export function SpeakersDialog({
  workspaceId,
  recordingId,
  segments,
  canIdentify,
  onSeek,
  onClose,
}: {
  workspaceId: string;
  recordingId: string;
  segments: MeetingSegment[];
  /** A language model is configured, so names can be looked for. */
  canIdentify: boolean;
  /** Seek playback to a position in ms (the time on a suggestion). */
  onSeek: (ms: number) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation(["meetings", "common"]);
  const toast = useToast();
  const assign = useAssignSpeakerByLabel(workspaceId, recordingId);
  const run = useSpeakerNameRun(workspaceId, recordingId, canIdentify);
  const identify = useIdentifySpeakerNames(workspaceId, recordingId);
  const decide = useDecideSpeakerName(workspaceId, recordingId);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const speakers = useMemo(() => {
    const groups = new Map<string, MeetingSegment[]>();
    for (const segment of segments) {
      const group = groups.get(segment.speaker_label) ?? [];
      group.push(segment);
      groups.set(segment.speaker_label, group);
    }
    return [...groups].map(([label, turns], index) => ({
      label,
      index,
      count: turns.length,
      // One line may have been corrected on its own: only a name every turn
      // shares is the speaker's name.
      name: turns.every((turn) => turn.speaker_name === turns[0].speaker_name)
        ? (turns[0].speaker_name ?? "")
        : "",
    }));
  }, [segments]);
  // Only what the person typed; untouched fields follow the transcript.
  const [typed, setTyped] = useState<Record<string, string>>({});
  const valueOf = (speaker: { label: string; name: string }) =>
    typed[speaker.label] ?? speaker.name;
  const changed = speakers.filter(
    (speaker) => valueOf(speaker).trim() !== speaker.name,
  );

  const working =
    identify.isPending ||
    run.data?.status === "pending" ||
    run.data?.status === "processing";
  const suggestions = (run.data?.suggestions ?? []).map(
    (suggestion, index) => ({
      ...suggestion,
      index,
    }),
  );
  const pending = suggestions.filter((s) => s.status === "pending");

  const startIdentify = () => {
    setError(null);
    identify.mutate(undefined, {
      onError: (cause) =>
        setError(getErrorMessage(cause, t("speakerNames.identifyFailed"))),
    });
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (changed.length === 0) return onClose();
    setError(null);
    setSaving(true);
    try {
      for (const speaker of changed) {
        const name = valueOf(speaker).trim();
        await assign.mutateAsync(
          name
            ? { speaker_label: speaker.label, speaker_name: name }
            : { speaker_label: speaker.label, clear_speaker: true },
        );
      }
      toast.success(t("speakerNames.saved", { count: changed.length }));
      onClose();
    } catch (cause) {
      setError(getErrorMessage(cause, t("speakerNames.saveFailed")));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={true} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[90dvh] flex-col sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("speakerNames.title")}</DialogTitle>
          <DialogDescription>{t("speakerNames.hint")}</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-1">
          <div className="flex flex-col gap-4">
            {error && (
              <Alert
                variant="danger"
                isInline={true}
                isLiveRegion={true}
                title={error}
              />
            )}
            {canIdentify && (
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={startIdentify}
                  disabled={working}
                  aria-busy={working}
                >
                  {working ? (
                    <Spinner data-icon="inline-start" aria-hidden="true" />
                  ) : (
                    <SparklesIcon data-icon="inline-start" />
                  )}
                  {t(
                    working
                      ? "speakerNames.identifying"
                      : "speakerNames.identify",
                  )}
                </Button>
                <span
                  className="text-sm text-muted-foreground"
                  aria-live="polite"
                >
                  {!working &&
                    run.data?.status === "ready" &&
                    (suggestions.length === 0
                      ? t("speakerNames.noneFound")
                      : t("speakerNames.found", { count: suggestions.length }))}
                </span>
              </div>
            )}
            {!working && run.data?.status === "failed" && (
              <Alert
                variant="danger"
                isInline={true}
                title={t("speakerNames.identifyFailed")}
              >
                {run.data.error}
              </Alert>
            )}
            <form id={FORM_ID} onSubmit={(event) => void submit(event)}>
              <FieldGroup>
                {speakers.map((speaker) => {
                  const friendly = speakerLabel(speaker.label, (number) =>
                    t("review.speaker", { number }),
                  );
                  const id = `speaker-name-${speaker.index}`;
                  return (
                    <Field key={speaker.label}>
                      <FieldLabel htmlFor={id}>
                        <span
                          className={cn(
                            "speaker-dot",
                            `speaker-color-${speaker.index % 6}`,
                          )}
                          aria-hidden="true"
                        />
                        {friendly}
                        <span className="speaker-turn-count font-normal">
                          {t("review.turns", { count: speaker.count })}
                        </span>
                      </FieldLabel>
                      <Input
                        id={id}
                        value={valueOf(speaker)}
                        maxLength={120}
                        placeholder={t("speakerNames.namePlaceholder")}
                        onChange={(event) => {
                          const value = event.currentTarget.value;
                          setTyped((names) => ({
                            ...names,
                            [speaker.label]: value,
                          }));
                        }}
                      />
                      {pending
                        .filter((s) => s.speaker_label === speaker.label)
                        .map((suggestion) => (
                          <div
                            key={suggestion.index}
                            className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border bg-muted/40 px-3 py-2 text-sm"
                          >
                            <span className="min-w-0 flex-1 basis-48">
                              <strong className="font-medium">
                                {suggestion.name}
                              </strong>{" "}
                              <span className="text-muted-foreground">
                                “{suggestion.quote}”
                              </span>
                            </span>
                            <Button
                              variant="secondary"
                              size="xs"
                              aria-label={t("speakerNames.listen", {
                                time: formatMsClock(suggestion.start_ms),
                              })}
                              onClick={() => onSeek(suggestion.start_ms)}
                            >
                              {formatMsClock(suggestion.start_ms)}
                            </Button>
                            {(["accept", "dismiss"] as const).map(
                              (decision) => (
                                <Button
                                  key={decision}
                                  variant="link"
                                  size="sm"
                                  disabled={decide.isPending}
                                  aria-label={t(
                                    `speakerNames.${decision}Aria`,
                                    {
                                      name: suggestion.name,
                                      speaker: friendly,
                                    },
                                  )}
                                  onClick={() =>
                                    decide.mutate(
                                      {
                                        runId: run.data!.id,
                                        index: suggestion.index,
                                        decision,
                                      },
                                      {
                                        onSuccess: () =>
                                          // The field follows the transcript again.
                                          setTyped(
                                            ({ [speaker.label]: _, ...rest }) =>
                                              rest,
                                          ),
                                        onError: (cause) =>
                                          setError(
                                            getErrorMessage(
                                              cause,
                                              t("speakerNames.saveFailed"),
                                            ),
                                          ),
                                      },
                                    )
                                  }
                                >
                                  {t(`speakerNames.${decision}`)}
                                </Button>
                              ),
                            )}
                          </div>
                        ))}
                    </Field>
                  );
                })}
              </FieldGroup>
            </form>
          </div>
        </div>
        <DialogFooter>
          <Button
            type="submit"
            form={FORM_ID}
            disabled={saving}
            aria-busy={saving}
          >
            {saving && <Spinner data-icon="inline-start" aria-hidden="true" />}
            {t("common:actions.save")}
          </Button>
          <Button variant="link" onClick={onClose}>
            {t("common:actions.cancel")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
