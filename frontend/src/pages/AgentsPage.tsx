import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { BotIcon, PlusIcon, XIcon } from "lucide-react";

import { getErrorMessage } from "@/api/client";
import type { NoteAgent } from "@/api/notes";
import { Alert } from "@/components/Alert";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { DataState } from "@/components/DataState";
import { FormFieldError } from "@/components/FormFieldError";
import { MarkdownEditor } from "@/components/MarkdownEditor";
import { PageHeader } from "@/components/PageHeader";
import { RowActions } from "@/components/RowActions";
import { useToast } from "@/components/toast/useToast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAuth } from "@/auth/useAuth";
import { useConfig } from "@/hooks/useConfig";
import {
  useCreateNoteAgent,
  useDeleteNoteAgent,
  useNoteAgents,
  useResetNoteAgent,
  useUpdateNoteAgent,
} from "@/hooks/useNotes";
import { formatRelativeTime } from "@/utils/relativeTime";

const FORM_ID = "agent-form";
const INSTRUCTIONS_MAX = 12000;

/** Create, edit or (for agents the user may not change) read an agent. */
function AgentDialog({
  agent,
  shared,
  onClose,
}: {
  /** The agent to edit or view; undefined = a new one. */
  agent?: NoteAgent;
  /** For a new agent: shared with everyone, or personal. */
  shared: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation(["notes", "common"]);
  const toast = useToast();
  const create = useCreateNoteAgent();
  const update = useUpdateNoteAgent();
  const reset = useResetNoteAgent();
  const readOnly = agent !== undefined && !agent.can_edit;
  const [name, setName] = useState(agent?.name ?? "");
  const [description, setDescription] = useState(agent?.description ?? "");
  const [instructions, setInstructions] = useState(agent?.instructions ?? "");
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const nameError = submitted && !name.trim() ? t("agents.nameRequired") : null;
  const instructionsError =
    submitted && !instructions.trim()
      ? t("agents.instructionsRequired")
      : instructions.length > INSTRUCTIONS_MAX
        ? t("agents.instructionsTooLong", { max: INSTRUCTIONS_MAX })
        : null;
  const busy = create.isPending || update.isPending || reset.isPending;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (!name.trim() || !instructions.trim()) return;
    if (instructions.length > INSTRUCTIONS_MAX) return;
    setError(null);
    const input = {
      name: name.trim(),
      description: description.trim() || null,
      instructions: instructions.trim(),
    };
    const options = {
      onSuccess: (saved: NoteAgent) => {
        toast.success(
          t(agent ? "agents.toastSaved" : "agents.toastCreated", {
            name: saved.name,
          }),
        );
        onClose();
      },
      onError: (cause: unknown) =>
        setError(getErrorMessage(cause, t("agents.saveFailed"))),
    };
    if (agent) update.mutate({ id: agent.id, ...input }, options);
    else create.mutate({ ...input, shared }, options);
  };

  const title = readOnly
    ? agent.name
    : agent
      ? t("agents.editTitle")
      : t(shared ? "agents.newSharedTitle" : "agents.newPersonalTitle");

  return (
    <Dialog open={true} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[90dvh] flex-col sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {readOnly ? t("agents.readOnlyHint") : t("agents.dialogHint")}
          </DialogDescription>
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
            <form id={FORM_ID} onSubmit={submit} noValidate={true}>
              <FieldGroup>
                <Field data-invalid={Boolean(nameError)}>
                  <FieldLabel htmlFor="agent-name">
                    {t("agents.name")}
                  </FieldLabel>
                  <Input
                    id="agent-name"
                    value={name}
                    maxLength={120}
                    readOnly={readOnly}
                    aria-invalid={Boolean(nameError)}
                    aria-describedby={
                      nameError ? "agent-name-error" : undefined
                    }
                    placeholder={t("agents.namePlaceholder")}
                    onChange={(event) => setName(event.currentTarget.value)}
                  />
                  <FormFieldError
                    id="agent-name-error"
                    message={nameError ?? undefined}
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="agent-description">
                    {t("agents.description")}
                  </FieldLabel>
                  <Input
                    id="agent-description"
                    value={description}
                    maxLength={500}
                    readOnly={readOnly}
                    placeholder={t("agents.descriptionPlaceholder")}
                    onChange={(event) =>
                      setDescription(event.currentTarget.value)
                    }
                  />
                </Field>
                <Field data-invalid={Boolean(instructionsError)}>
                  <FieldLabel htmlFor="agent-instructions">
                    {t("agents.instructions")}
                  </FieldLabel>
                  {readOnly ? (
                    <pre
                      id="agent-instructions"
                      className="max-h-96 overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-sm whitespace-pre-wrap"
                    >
                      {instructions}
                    </pre>
                  ) : (
                    <MarkdownEditor
                      id="agent-instructions"
                      value={instructions}
                      onChange={setInstructions}
                      label={t("agents.instructions")}
                      placeholder={t("agents.instructionsPlaceholder")}
                      rows={14}
                      showPreview={false}
                      invalid={Boolean(instructionsError)}
                      describedBy="agent-instructions-help"
                    />
                  )}
                  <FieldDescription id="agent-instructions-help">
                    {t("agents.instructionsHelp")}
                  </FieldDescription>
                  <FormFieldError
                    id="agent-instructions-error"
                    message={instructionsError ?? undefined}
                  />
                </Field>
              </FieldGroup>
            </form>
          </div>
        </div>
        <DialogFooter>
          {!readOnly && (
            <Button
              type="submit"
              form={FORM_ID}
              disabled={busy}
              aria-busy={busy}
            >
              {(create.isPending || update.isPending) && (
                <Spinner data-icon="inline-start" />
              )}
              {t(agent ? "common:actions.save" : "common:actions.create")}
            </Button>
          )}
          {agent?.builtin && agent.can_edit && agent.modified && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                reset.mutate(agent.id, {
                  onSuccess: () => {
                    toast.success(t("agents.toastReset", { name: agent.name }));
                    onClose();
                  },
                  onError: (cause) =>
                    setError(getErrorMessage(cause, t("agents.saveFailed"))),
                })
              }
            >
              {t("agents.reset")}
            </Button>
          )}
          <Button onClick={onClose} variant="link">
            {t(readOnly ? "common:actions.close" : "common:actions.cancel")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AgentTable({
  agents,
  onOpen,
  onDelete,
  onError,
}: {
  agents: NoteAgent[];
  onOpen: (agent: NoteAgent) => void;
  onDelete: (agent: NoteAgent) => void;
  onError: (message: string) => void;
}) {
  const { t, i18n } = useTranslation(["notes", "common"]);
  const update = useUpdateNoteAgent();
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{t("agents.name")}</TableHead>
          <TableHead className="hidden md:table-cell">
            {t("agents.description")}
          </TableHead>
          <TableHead>{t("agents.active")}</TableHead>
          <TableHead className="hidden sm:table-cell">
            {t("agents.changed")}
          </TableHead>
          <TableHead>
            <span className="sr-only">{t("agents.actionsColumn")}</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {agents.map((agent) => (
          <TableRow key={agent.id}>
            <TableCell>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="link"
                  className="h-auto p-0 text-left font-medium whitespace-normal"
                  onClick={() => onOpen(agent)}
                >
                  {agent.name}
                </Button>
                {agent.builtin && (
                  <Badge variant="secondary">{t("agents.builtin")}</Badge>
                )}
                {agent.builtin && agent.modified && (
                  <Badge variant="outline">{t("agents.modified")}</Badge>
                )}
              </div>
            </TableCell>
            <TableCell className="hidden max-w-md text-muted-foreground md:table-cell">
              <span className="line-clamp-2 whitespace-normal">
                {agent.description}
              </span>
            </TableCell>
            <TableCell>
              <Switch
                checked={agent.is_active}
                disabled={!agent.can_edit || update.isPending}
                aria-label={t("agents.activeAria", { name: agent.name })}
                onCheckedChange={(checked) =>
                  update.mutate(
                    { id: agent.id, is_active: checked },
                    {
                      onError: (cause) =>
                        onError(getErrorMessage(cause, t("agents.saveFailed"))),
                    },
                  )
                }
              />
            </TableCell>
            <TableCell className="hidden text-muted-foreground sm:table-cell">
              {formatRelativeTime(agent.updated_at, i18n.language)}
            </TableCell>
            <TableCell className="text-right">
              <RowActions
                toggleAriaLabel={t("agents.rowActions", { name: agent.name })}
                items={[
                  {
                    title: agent.can_edit
                      ? t("common:actions.edit")
                      : t("agents.view"),
                    onClick: () => onOpen(agent),
                  },
                  ...(agent.can_edit && !agent.builtin
                    ? [
                        { isSeparator: true },
                        {
                          title: t("common:actions.delete"),
                          isDanger: true,
                          onClick: () => onDelete(agent),
                        },
                      ]
                    : []),
                ]}
              />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** Manage note agents: the shared list and the user's own. */
export function AgentsPage() {
  const { t } = useTranslation(["notes", "common"]);
  const toast = useToast();
  const { isPrivileged } = useAuth();
  const { data: config } = useConfig();
  const agents = useNoteAgents();
  const remove = useDeleteNoteAgent();
  const [dialog, setDialog] = useState<{
    agent?: NoteAgent;
    shared: boolean;
  } | null>(null);
  const [deleting, setDeleting] = useState<NoteAgent | null>(null);
  const [error, setError] = useState<string | null>(null);

  const all = agents.data ?? [];
  const sections = [
    {
      key: "shared",
      shared: true,
      agents: all.filter((agent) => agent.shared),
      canAdd: isPrivileged,
    },
    {
      key: "mine",
      shared: false,
      agents: all.filter((agent) => !agent.shared),
      canAdd: true,
    },
  ] as const;

  return (
    <>
      <PageHeader
        title={t("agents.title")}
        description={t("agents.subtitle")}
        actions={
          <Button onClick={() => setDialog({ shared: false })}>
            <PlusIcon data-icon="inline-start" />
            {t("agents.new")}
          </Button>
        }
      />
      <section className="page-section">
        <div className="flex flex-col gap-4">
          {config && config.notes_status !== "available" && (
            <Alert
              variant="info"
              isInline={true}
              title={t("panel.unavailableTitle")}
            >
              {t("panel.unavailableBody")}
            </Alert>
          )}
          {error && (
            <Alert
              variant="danger"
              isInline={true}
              isLiveRegion={true}
              title={error}
              actionClose={
                <Button
                  aria-label={t("common:actions.close")}
                  variant="ghost"
                  size="icon-xs"
                  onClick={() => setError(null)}
                >
                  <XIcon />
                </Button>
              }
            />
          )}
          {sections.map((section) => (
            <Card key={section.key} className="gap-0 overflow-hidden py-0">
              <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 px-4 py-3">
                <div className="min-w-0">
                  <CardTitle className="text-base">
                    {t(`agents.sections.${section.key}.title`)}
                  </CardTitle>
                  <p className="text-sm text-muted-foreground">
                    {t(`agents.sections.${section.key}.hint`)}
                  </p>
                </div>
                {section.shared && section.canAdd && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setDialog({ shared: true })}
                  >
                    <PlusIcon data-icon="inline-start" />
                    {t("agents.newShared")}
                  </Button>
                )}
              </CardHeader>
              <CardContent className="border-t px-0">
                <DataState
                  isLoading={agents.isLoading}
                  error={agents.error}
                  onRetry={() => void agents.refetch()}
                  isEmpty={section.agents.length === 0}
                  emptyTitle={t(`agents.sections.${section.key}.emptyTitle`)}
                  emptyBody={t(`agents.sections.${section.key}.emptyBody`)}
                  emptyIcon={BotIcon}
                  headingLevel="h3"
                  skeleton={{ rows: 2 }}
                >
                  <AgentTable
                    agents={[...section.agents]}
                    onOpen={(agent) =>
                      setDialog({ agent, shared: agent.shared })
                    }
                    onDelete={setDeleting}
                    onError={setError}
                  />
                </DataState>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>
      {dialog && (
        <AgentDialog
          key={dialog.agent?.id ?? `new-${dialog.shared}`}
          agent={dialog.agent}
          shared={dialog.shared}
          onClose={() => setDialog(null)}
        />
      )}
      {deleting && (
        <ConfirmDeleteModal
          isOpen={true}
          title={t("agents.deleteTitle")}
          itemName={deleting.name}
          consequence={t("agents.deleteConsequence")}
          isDeleting={remove.isPending}
          onConfirm={() =>
            remove.mutate(deleting.id, {
              onSuccess: () => {
                toast.success(
                  t("agents.toastDeleted", { name: deleting.name }),
                );
                setDeleting(null);
              },
              onError: (cause) => {
                setDeleting(null);
                setError(getErrorMessage(cause, t("agents.deleteFailed")));
              },
            })
          }
          onClose={() => setDeleting(null)}
        />
      )}
    </>
  );
}
