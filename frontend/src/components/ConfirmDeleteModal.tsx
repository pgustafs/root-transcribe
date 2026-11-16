import {
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogFooter,
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogCancel,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useRef, type ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";

import { Alert } from "@/components/Alert";

type ConfirmDeleteModalProps = {
  isOpen: boolean;
  /** Name of the object being deleted, shown in bold. */
  itemName: string;
  /** Modal title, phrased as a question, e.g. "Delete user?". */
  title?: string;
  /** Extra consequence text shown after the standard sentence. */
  consequence?: ReactNode;
  /** Separate warning callout (e.g. cascading deletes). */
  warning?: ReactNode;
  /** Label of the confirming button; defaults to "Delete". */
  confirmLabel?: string;
  isDeleting?: boolean;
  onConfirm: () => void;
  onClose: () => void;
};

export function ConfirmDeleteModal({
  isOpen,
  itemName,
  title,
  consequence,
  warning,
  confirmLabel,
  isDeleting = false,
  onConfirm,
  onClose,
}: ConfirmDeleteModalProps) {
  const { t } = useTranslation("common");
  // The safe choice gets focus when the dialog opens (Enter must not delete).
  const cancelRef = useRef<HTMLButtonElement>(null);
  return (
    <AlertDialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <AlertDialogContent
        aria-labelledby="confirm-delete-title"
        aria-describedby="confirm-delete-body"
        initialFocus={cancelRef}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
      >
        <AlertDialogHeader>
          <AlertDialogTitle id="confirm-delete-title">
            {title ?? t("confirmDelete.defaultTitle")}
          </AlertDialogTitle>
        </AlertDialogHeader>
        <AlertDialogDescription
          render={<div id="confirm-delete-body" className="min-w-0" />}
        >
          <div className="flex flex-col gap-4">
            <div>
              <p className="app-copy">
                <Trans
                  i18nKey="common:confirmDelete.body"
                  values={{ name: itemName }}
                  components={{ strong: <strong /> }}
                />
                {consequence ? <> {consequence}</> : null}
              </p>
            </div>
            {warning && (
              <div>
                <Alert variant="warning" isInline={true} title={warning} />
              </div>
            )}
          </div>
        </AlertDialogDescription>
        <AlertDialogFooter>
          <Button
            onClick={onConfirm}
            disabled={isDeleting}
            aria-busy={isDeleting}
            variant="destructive"
          >
            {isDeleting && (
              <Spinner data-icon="inline-start" aria-label={undefined} />
            )}
            {confirmLabel ?? t("actions.delete")}
          </Button>
          {/* Cancel stays enabled mid-delete: closing only dismisses the
            dialog, the request settles on its own and reports via the caller. */}
          <AlertDialogCancel ref={cancelRef}>
            {t("actions.cancel")}
          </AlertDialogCancel>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
