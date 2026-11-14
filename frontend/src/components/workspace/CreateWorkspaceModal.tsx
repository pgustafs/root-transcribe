import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Field, FieldLabel, FieldGroup } from "@/components/ui/field";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Alert } from "@/components/Alert";
import { FormFieldError } from "@/components/FormFieldError";
export type CreateWorkspacePayload = {
  name: string;
  description: string | null;
};
type Props = {
  submitError: string | null;
  isSubmitting: boolean;
  onSubmit: (payload: CreateWorkspacePayload) => void;
  onClose: () => void;
};
const TITLE_ID = "create-workspace-title";
const FORM_ID = "create-workspace-form";
const NAME_ERROR_ID = "ws-name-error";
export function CreateWorkspaceModal({
  submitError,
  isSubmitting,
  onSubmit,
  onClose,
}: Props) {
  const { t } = useTranslation(["access", "common"]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) {
      setNameError(t("access:workspaces.nameRequired"));
      return;
    }
    setNameError(null);
    onSubmit({ name: name.trim(), description: description.trim() || null });
  };
  return (
    <Dialog
      open={true}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        aria-labelledby={TITLE_ID}
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle id={TITLE_ID}>
            {t("access:workspaces.create")}
          </DialogTitle>
        </DialogHeader>
        <div className="min-w-0">
          <div className="flex flex-col gap-4">
            {submitError && (
              <div>
                {/* On failure (e.g. 409 duplicate name) the dialog stays open
                    and shows the error - it must never fail silently. */}
                <Alert
                  variant="danger"
                  isInline={true}
                  isLiveRegion={true}
                  title={submitError}
                />
              </div>
            )}
            <div>
              <form id={FORM_ID} onSubmit={submit} noValidate={true}>
                <FieldGroup>
                  <Field data-invalid={Boolean(nameError)}>
                    <FieldLabel htmlFor="ws-name">
                      {t("access:workspaces.name")}{" "}
                      <span aria-hidden="true">*</span>
                    </FieldLabel>
                    <Input
                      id="ws-name"
                      aria-invalid={Boolean(nameError)}
                      value={name}
                      placeholder={t("access:workspaces.namePlaceholder")}
                      aria-describedby={nameError ? NAME_ERROR_ID : undefined}
                      required={true}
                      onChange={(_e) => {
                        const v = _e.currentTarget.value;
                        return setName(v);
                      }}
                    />
                    <FormFieldError
                      id={NAME_ERROR_ID}
                      message={nameError ?? undefined}
                    />
                  </Field>
                  <Field>
                    <FieldLabel htmlFor="ws-desc">
                      {t("access:workspaces.description")}
                    </FieldLabel>
                    <Input
                      id="ws-desc"
                      value={description}
                      onChange={(_e) => {
                        const v = _e.currentTarget.value;
                        return setDescription(v);
                      }}
                    />
                  </Field>
                </FieldGroup>
              </form>
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button
            type="submit"
            form={FORM_ID}
            disabled={isSubmitting}
            aria-busy={isSubmitting}
            variant="default"
          >
            {isSubmitting && (
              <Spinner data-icon="inline-start" aria-label={undefined} />
            )}
            {t("common:actions.create")}
          </Button>
          <Button onClick={onClose} variant="link">
            {t("common:actions.cancel")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
