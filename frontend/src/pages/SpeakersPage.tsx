import {
  MicIcon as MicrophoneIcon,
  XIcon,
  ChevronDownIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  TableHead,
  TableRow,
  TableHeader,
  TableCell,
  TableBody,
  Table,
} from "@/components/ui/table";
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from "@/components/ui/collapsible";
import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Field, FieldLabel, FieldGroup } from "@/components/ui/field";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { Spinner } from "@/components/ui/spinner";

import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import type { SpeakerProfile } from "@/api/types";
import { useAuth } from "@/auth/useAuth";
import { Alert } from "@/components/Alert";
import { ComplianceExportsCard } from "@/components/ComplianceExportsCard";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { DataState } from "@/components/DataState";
import { FormFieldError } from "@/components/FormFieldError";
import { PageHeader } from "@/components/PageHeader";
import { RowActions } from "@/components/RowActions";
import { StatusLabel } from "@/components/StatusLabel";
import { useToast } from "@/components/toast/useToast";
import { VoiceEnrollModal } from "@/components/VoiceEnrollModal";
import { useConfig } from "@/hooks/useConfig";
import {
  useCreateSpeaker,
  useDeleteSpeaker,
  useEnrollVoiceprint,
  useSpeakers,
  useUpdateSpeaker,
} from "@/hooks/useSpeakers";
import { neutral, success, type StatusPresentation } from "@/lib/labels";

const SPEAKER_STATUS: Record<SpeakerProfile["status"], StatusPresentation> = {
  active: success("meetings:speakers.active"),
  revoked: neutral("meetings:speakers.revoked"),
};

export function SpeakersPage() {
  const { t } = useTranslation(["meetings", "common"]);
  const toast = useToast();
  const { isPrivileged, isAdmin } = useAuth();
  const { data: config } = useConfig();
  // After a full shutdown the page stays reachable so admins can discover and
  // ERASE retained biometric data (issue #14 review) — but the PROCESSING
  // actions (add, enroll, activate/deactivate) hit gated endpoints that 503,
  // so present a read/erase-only mode instead of buttons guaranteed to fail.
  const processingEnabled = Boolean(config?.meetings_enabled);
  const canAdd = isPrivileged && processingEnabled;

  const speakers = useSpeakers();
  const update = useUpdateSpeaker();
  const del = useDeleteSpeaker();
  const enroll = useEnrollVoiceprint();

  const [addOpen, setAddOpen] = useState(false);
  const [privacyOpen, setPrivacyOpen] = useState(false);
  const [deleting, setDeleting] = useState<SpeakerProfile | null>(null);
  const [enrolling, setEnrolling] = useState<SpeakerProfile | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const list = speakers.data ?? [];

  const toggleStatus = (sp: SpeakerProfile) => {
    const activate = sp.status !== "active";
    setActionError(null);
    update.mutate(
      { id: sp.id, status: activate ? "active" : "revoked" },
      {
        onSuccess: () =>
          toast.success(
            t(
              activate
                ? "speakers.toastActivated"
                : "speakers.toastDeactivated",
              { name: sp.display_name },
            ),
          ),
        onError: (error) =>
          setActionError(getErrorMessage(error, t("speakers.updateError"))),
      },
    );
  };

  const confirmDelete = () => {
    if (!deleting) return;
    const name = deleting.display_name;
    del.mutate(deleting.id, {
      onSuccess: () => {
        setDeleting(null);
        toast.success(t("speakers.toastDeleted", { name }));
      },
      onError: (error) => {
        setDeleting(null);
        setActionError(getErrorMessage(error, t("speakers.deleteError")));
      },
    });
  };

  const addButton = canAdd ? (
    <Button onClick={() => setAddOpen(true)} variant="default">
      {t("speakers.add")}
    </Button>
  ) : undefined;

  return (
    <>
      <PageHeader
        title={t("speakers.title")}
        description={t("speakers.subtitle")}
        actions={addButton}
      />
      <section className="page-section flex-1 min-h-0">
        <div className="flex flex-col gap-4">
          {isPrivileged && !processingEnabled && (
            <div>
              <Alert
                variant="info"
                isInline={true}
                title={t("speakers.shutdownTitle")}
              >
                {t("speakers.shutdownNotice")}
              </Alert>
            </div>
          )}
          {actionError && (
            <div>
              <Alert
                variant="danger"
                isInline={true}
                isLiveRegion={true}
                title={actionError}
                actionClose={
                  <Button
                    aria-label={t("common:actions.close")}
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => setActionError(null)}
                  >
                    <XIcon data-icon="inline-start" />
                  </Button>
                }
              />
            </div>
          )}
          <div>
            <DataState
              isLoading={speakers.isLoading}
              error={speakers.error}
              onRetry={() => void speakers.refetch()}
              isEmpty={list.length === 0}
              emptyTitle={t("speakers.empty.title")}
              emptyBody={t("speakers.empty.body")}
              emptyIcon={MicrophoneIcon}
              emptyAction={addButton}
              loadingLabel={t("speakers.loading")}
              skeleton={{ rows: 3 }}
            >
              <Table aria-label={t("speakers.title")}>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("speakers.name")}</TableHead>
                    <TableHead>{t("speakers.voiceprints")}</TableHead>
                    <TableHead>{t("speakers.status")}</TableHead>
                    {isPrivileged && (
                      <TableHead>
                        <span className="sr-only">
                          {t("speakers.actionsLabel")}
                        </span>
                      </TableHead>
                    )}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {list.map((sp) => (
                    <TableRow key={sp.id}>
                      <TableCell>{sp.display_name}</TableCell>
                      <TableCell>{sp.voiceprint_count}</TableCell>
                      <TableCell>
                        <StatusLabel
                          presentation={SPEAKER_STATUS[sp.status]}
                          value={sp.status}
                          isCompact={true}
                        />
                      </TableCell>
                      {isPrivileged && (
                        <TableCell>
                          <RowActions
                            toggleAriaLabel={t("speakers.actionsFor", {
                              name: sp.display_name,
                            })}
                            items={[
                              // Processing actions are hidden after a full
                              // shutdown — they would 503. Erasure (a right)
                              // always stays (issue #14 review).
                              ...(processingEnabled
                                ? [
                                    {
                                      title: t("speakers.enroll"),
                                      // A deactivated profile is not enrollable —
                                      // the worker refuses it too (issue #14).
                                      isDisabled:
                                        !sp.consent_granted_at ||
                                        sp.status !== "active",
                                      onClick: () => setEnrolling(sp),
                                    },
                                    {
                                      title:
                                        sp.status === "active"
                                          ? t("speakers.deactivate")
                                          : t("speakers.activate"),
                                      onClick: () => toggleStatus(sp),
                                    },
                                  ]
                                : []),
                              {
                                title: t("speakers.delete"),
                                isDanger: true,
                                onClick: () => setDeleting(sp),
                              },
                            ]}
                          />
                        </TableCell>
                      )}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </DataState>
          </div>
          {isAdmin && (
            <div>
              <Collapsible
                open={privacyOpen}
                onOpenChange={(checked) => {
                  const expanded = checked;
                  return setPrivacyOpen(expanded);
                }}
              >
                <CollapsibleTrigger asChild={true}>
                  <Button variant="ghost" size="sm">
                    {t("speakers.privacy")}
                    <ChevronDownIcon data-icon="inline-end" />
                  </Button>
                </CollapsibleTrigger>
                <CollapsibleContent className="pt-3">
                  {privacyOpen && <ComplianceExportsCard />}
                </CollapsibleContent>
              </Collapsible>
            </div>
          )}
        </div>
      </section>

      {addOpen && (
        <AddSpeakerModal
          onClose={() => setAddOpen(false)}
          onAdded={(name) => {
            setAddOpen(false);
            toast.success(t("speakers.toastAdded", { name }));
          }}
        />
      )}

      {deleting && (
        <ConfirmDeleteModal
          isOpen={true}
          itemName={deleting.display_name}
          title={t("speakers.deleteTitle")}
          consequence={t("speakers.deleteConsequence")}
          isDeleting={del.isPending}
          onConfirm={confirmDelete}
          onClose={() => setDeleting(null)}
        />
      )}

      {enrolling && (
        <VoiceEnrollModal
          isOpen={true}
          title={t("speakers.enrollFor", { name: enrolling.display_name })}
          onClose={() => setEnrolling(null)}
          onSubmit={async (blob, filename) => {
            await enroll.mutateAsync({
              id: enrolling.id,
              file: blob,
              filename,
            });
            toast.success(t("speakers.enrolledToast"));
          }}
        />
      )}
    </>
  );
}

function AddSpeakerModal({
  onClose,
  onAdded,
}: {
  onClose: () => void;
  onAdded: (name: string) => void;
}) {
  const { t } = useTranslation(["meetings", "common", "validation"]);
  const create = useCreateSpeaker();
  const { isAdmin } = useAuth();
  const [voiceOptions, setVoiceOptions] = useState(false);
  const [evidence, setEvidence] = useState("");
  const [name, setName] = useState("");
  const [consent, setConsent] = useState(false);
  const [nameError, setNameError] = useState<string | undefined>(undefined);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setNameError(t("validation:nameRequired"));
      return;
    }
    if (consent && !evidence.trim()) {
      setSubmitError(t("speakers.addModal.evidenceRequired"));
      return;
    }
    setSubmitError(null);
    create.mutate(
      {
        display_name: trimmed,
        consent_granted: consent,
        ...(consent ? { consent_evidence: evidence.trim() } : {}),
      },
      {
        onSuccess: () => onAdded(trimmed),
        onError: (error) =>
          setSubmitError(getErrorMessage(error, t("speakers.saveError"))),
      },
    );
  };

  return (
    <Dialog
      open={true}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        aria-labelledby="add-speaker-title"
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle id="add-speaker-title">
            {t("speakers.addModal.title")}
          </DialogTitle>
        </DialogHeader>
        <div className="min-w-0">
          <form id="add-speaker-form" onSubmit={submit} noValidate={true}>
            <FieldGroup>
              <p className="app-copy">{t("speakers.addModal.help")}</p>
              {submitError && (
                <Alert
                  variant="danger"
                  isInline={true}
                  isLiveRegion={true}
                  title={submitError}
                />
              )}
              <Field data-invalid={Boolean(nameError)}>
                <FieldLabel htmlFor="speaker-name">
                  {t("speakers.name")} <span aria-hidden="true">*</span>
                </FieldLabel>
                <Input
                  id="speaker-name"
                  maxLength={255}
                  value={name}
                  aria-invalid={Boolean(nameError)}
                  aria-describedby={
                    nameError ? "speaker-name-error" : undefined
                  }
                  onChange={(_e) => {
                    const v = _e.currentTarget.value;
                    setName(v);
                    if (v.trim()) setNameError(undefined);
                  }}
                />
                <FormFieldError id="speaker-name-error" message={nameError} />
              </Field>
              {isAdmin && (
                <Collapsible
                  open={voiceOptions}
                  onOpenChange={(checked) => {
                    const expanded = checked;
                    return setVoiceOptions(expanded);
                  }}
                >
                  <CollapsibleTrigger asChild={true}>
                    <Button variant="ghost" size="sm">
                      {t("speakers.addModal.voiceOptions")}
                      <ChevronDownIcon data-icon="inline-end" />
                    </Button>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="pt-3">
                    <div className="flex flex-col gap-4">
                      <Field>
                        <Field orientation="horizontal">
                          <Checkbox
                            id="speaker-consent"
                            checked={consent}
                            onCheckedChange={(state) => {
                              const checked = state === true;
                              return setConsent(checked);
                            }}
                          />
                          <FieldLabel htmlFor="speaker-consent">
                            {t("speakers.addModal.consent")}
                          </FieldLabel>
                        </Field>
                      </Field>
                      {consent && (
                        <Field>
                          <FieldLabel htmlFor="speaker-evidence">
                            {t("speakers.addModal.evidence")}{" "}
                            <span aria-hidden="true">*</span>
                          </FieldLabel>
                          <Textarea
                            id="speaker-evidence"
                            value={evidence}
                            maxLength={1000}
                            placeholder={t("speakers.addModal.evidenceHelp")}
                            required={true}
                            onChange={(_event) => {
                              const value = _event.currentTarget.value;
                              return setEvidence(value);
                            }}
                          />
                        </Field>
                      )}
                    </div>
                  </CollapsibleContent>
                </Collapsible>
              )}
            </FieldGroup>
          </form>
        </div>
        <DialogFooter>
          <Button
            type="submit"
            form="add-speaker-form"
            disabled={create.isPending}
            aria-busy={create.isPending}
            variant="default"
          >
            {create.isPending && (
              <Spinner data-icon="inline-start" aria-label={undefined} />
            )}
            {t("common:actions.save")}
          </Button>
          <Button onClick={onClose} disabled={create.isPending} variant="link">
            {t("common:actions.cancel")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
