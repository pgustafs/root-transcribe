import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import {
  CopyIcon,
  DownloadIcon,
  MoreVerticalIcon,
  NotebookPenIcon,
  PencilIcon,
  RefreshCwIcon,
  XIcon,
} from "lucide-react";

import { getErrorMessage } from "@/api/client";
import {
  exportNote,
  type MeetingNote,
  type NoteAgent,
  type NoteExportFormat,
} from "@/api/notes";
import { Alert } from "@/components/Alert";
import { BrandMark } from "@/components/BrandMark";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { DataState } from "@/components/DataState";
import { Markdown } from "@/components/Markdown";
import { MarkdownEditor } from "@/components/MarkdownEditor";
import { useToast } from "@/components/toast/useToast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Spinner } from "@/components/ui/spinner";
import {
  useCreateNote,
  useDeleteNote,
  useNoteAgents,
  useNotes,
  useRestoreNote,
  useUpdateNote,
} from "@/hooks/useNotes";
import { downloadBlob } from "@/utils/downloadBlob";
import { formatRelativeTime } from "@/utils/relativeTime";

const EXPORT_FORMATS: NoteExportFormat[] = ["md", "docx", "pdf"];

/** "part:2/4" → { step: "part", n: 2, total: 4 } for the progress text. */
function parseStep(step: string | null) {
  const match = /^part:(\d+)\/(\d+)$/.exec(step ?? "");
  if (match)
    return { step: "part", n: Number(match[1]), total: Number(match[2]) };
  return { step: step === "merge" ? "merge" : "write", n: 0, total: 0 };
}

function NoteCard({
  note,
  workspaceId,
  recordingId,
  canEdit,
  canExport,
  canRunAgain,
  isOutdated,
  onRunAgain,
}: {
  note: MeetingNote;
  workspaceId: string;
  recordingId: string;
  canEdit: boolean;
  canExport: boolean;
  /** The agent still exists, is switched on and notes can be created. */
  canRunAgain: boolean;
  /** Written from an older version of the transcript. */
  isOutdated: boolean;
  onRunAgain: () => void;
}) {
  const { t, i18n } = useTranslation(["notes", "common"]);
  const toast = useToast();
  const update = useUpdateNote(workspaceId, recordingId);
  const restore = useRestoreNote(workspaceId, recordingId);
  const remove = useDeleteNote(workspaceId, recordingId);
  const [draft, setDraft] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const working = note.status === "pending" || note.status === "processing";
  const text = note.content_markdown ?? "";
  const progress = parseStep(note.current_step);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(t("panel.copied"));
    } catch {
      setError(t("panel.copyFailed"));
    }
  };

  const download = async (format: NoteExportFormat) => {
    setError(null);
    setExporting(true);
    try {
      const { blob, filename } = await exportNote(
        workspaceId,
        recordingId,
        note.id,
        format,
      );
      downloadBlob(blob, filename ?? `${note.agent_name}.${format}`);
    } catch (cause) {
      setError(getErrorMessage(cause, t("panel.exportFailed")));
    } finally {
      setExporting(false);
    }
  };

  const save = () => {
    if (draft === null) return;
    setError(null);
    update.mutate(
      {
        noteId: note.id,
        content_markdown: draft,
        expected_version: note.version,
      },
      {
        onSuccess: () => {
          setDraft(null);
          toast.success(t("panel.saved"));
        },
        onError: (cause) =>
          setError(getErrorMessage(cause, t("panel.saveFailed"))),
      },
    );
  };

  return (
    <article
      className="rounded-xl border bg-card"
      aria-label={t("panel.noteAria", { agent: note.agent_name })}
      aria-busy={working}
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
        {working ? (
          <Spinner className="size-7" aria-hidden="true" />
        ) : (
          <BrandMark className="size-7 shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-heading text-sm font-semibold">
            {note.agent_name}
          </h3>
          <p className="truncate text-xs text-muted-foreground">
            {[
              formatRelativeTime(note.created_at, i18n.language),
              note.created_by_email,
              note.model,
            ]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
        {note.edited && <Badge variant="outline">{t("panel.edited")}</Badge>}
        {note.status === "ready" && draft === null && (
          <div className="flex items-center gap-0.5">
            <Button variant="ghost" size="sm" onClick={() => void copy()}>
              <CopyIcon data-icon="inline-start" />
              {t("panel.copy")}
            </Button>
            {canExport && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={exporting}
                    aria-busy={exporting}
                  >
                    {exporting ? (
                      <Spinner data-icon="inline-start" />
                    ) : (
                      <DownloadIcon data-icon="inline-start" />
                    )}
                    {t("panel.export")}
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuGroup>
                    {EXPORT_FORMATS.map((format) => (
                      <DropdownMenuItem
                        key={format}
                        onSelect={() => void download(format)}
                      >
                        {t(`panel.formats.${format}`)}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuGroup>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            {canEdit && (
              <Button variant="ghost" size="sm" onClick={() => setDraft(text)}>
                <PencilIcon data-icon="inline-start" />
                {t("common:actions.edit")}
              </Button>
            )}
          </div>
        )}
        {canEdit && !working && draft === null && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={t("panel.moreActions", { agent: note.agent_name })}
                className="text-muted-foreground"
              >
                <MoreVerticalIcon />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                <DropdownMenuItem disabled={!canRunAgain} onSelect={onRunAgain}>
                  {t("panel.runAgain")}
                </DropdownMenuItem>
                {note.edited && (
                  <DropdownMenuItem
                    onSelect={() =>
                      restore.mutate(note.id, {
                        onSuccess: () => toast.success(t("panel.restored")),
                        onError: (cause) =>
                          setError(
                            getErrorMessage(cause, t("panel.saveFailed")),
                          ),
                      })
                    }
                  >
                    {t("panel.restore")}
                  </DropdownMenuItem>
                )}
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={() => setConfirmDelete(true)}
                >
                  {t("common:actions.delete")}
                </DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </header>
      <div className="border-t px-4 py-4">
        {error && (
          <Alert
            variant="danger"
            isInline={true}
            isLiveRegion={true}
            title={error}
            className="mb-3"
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
        {working && (
          <p role="status" className="text-sm text-muted-foreground">
            {t(`panel.steps.${progress.step}`, {
              n: progress.n,
              total: progress.total,
            })}
          </p>
        )}
        {note.status === "failed" && (
          <Alert
            variant="danger"
            isInline={true}
            title={t("panel.failed")}
            actionClose={
              canRunAgain ? (
                <Button variant="outline" size="sm" onClick={onRunAgain}>
                  <RefreshCwIcon data-icon="inline-start" />
                  {t("panel.tryAgain")}
                </Button>
              ) : undefined
            }
          >
            {note.error}
          </Alert>
        )}
        {note.status === "ready" && isOutdated && draft === null && (
          <p className="mb-3 text-xs text-muted-foreground">
            {t("panel.outdated")}
          </p>
        )}
        {note.status === "ready" &&
          (draft === null ? (
            <Markdown>{text}</Markdown>
          ) : (
            <div className="flex flex-col gap-3">
              <MarkdownEditor
                value={draft}
                onChange={setDraft}
                label={t("panel.editorLabel", { agent: note.agent_name })}
              />
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  onClick={save}
                  disabled={update.isPending || draft === text}
                  aria-busy={update.isPending}
                >
                  {update.isPending && <Spinner data-icon="inline-start" />}
                  {t("common:actions.save")}
                </Button>
                <Button variant="link" size="sm" onClick={() => setDraft(null)}>
                  {t("common:actions.cancel")}
                </Button>
              </div>
            </div>
          ))}
      </div>
      {confirmDelete && (
        <ConfirmDeleteModal
          isOpen={true}
          title={t("panel.deleteTitle")}
          itemName={note.agent_name}
          consequence={t("panel.deleteConsequence")}
          isDeleting={remove.isPending}
          onConfirm={() =>
            remove.mutate(note.id, {
              onSuccess: () => {
                setConfirmDelete(false);
                toast.success(t("panel.deleted"));
              },
              onError: (cause) => {
                setConfirmDelete(false);
                setError(getErrorMessage(cause, t("panel.deleteFailed")));
              },
            })
          }
          onClose={() => setConfirmDelete(false)}
        />
      )}
    </article>
  );
}

/**
 * The Notes tab of a recording: pick an agent to write a document from the
 * transcript, then read, edit and export what it wrote.
 */
export function MeetingNotesPanel({
  workspaceId,
  recordingId,
  displayRevisionId,
  canCreate,
  canExport,
  isAvailable,
}: {
  workspaceId: string;
  recordingId: string;
  /** The transcript version on screen, to flag notes written from an older one. */
  displayRevisionId: string | null;
  /** The caller holds the create_notes capability (server-computed). */
  canCreate: boolean;
  canExport: boolean;
  /** An LLM is configured (config.notes_status). */
  isAvailable: boolean;
}) {
  const { t } = useTranslation(["notes", "common"]);
  const notes = useNotes(workspaceId, recordingId);
  const agents = useNoteAgents(canCreate && isAvailable);
  const create = useCreateNote(workspaceId, recordingId);
  const [startError, setStartError] = useState<string | null>(null);

  const rows = notes.data ?? [];
  const active = (agents.data ?? []).filter((agent) => agent.is_active);
  const groups: { key: string; agents: NoteAgent[] }[] = [
    { key: "shared", agents: active.filter((agent) => agent.shared) },
    { key: "mine", agents: active.filter((agent) => !agent.shared) },
  ].filter((group) => group.agents.length > 0);
  const busyAgents = new Set(
    rows
      .filter((n) => n.status === "pending" || n.status === "processing")
      .map((n) => n.agent_id),
  );

  const run = (agentId: string) => {
    setStartError(null);
    create.mutate(agentId, {
      onError: (cause) =>
        setStartError(getErrorMessage(cause, t("panel.startFailed"))),
    });
  };

  // Newest note per agent on top; its earlier versions fold away below it.
  const seen = new Set<string>();
  const latest: MeetingNote[] = [];
  const earlier = new Map<string, MeetingNote[]>();
  for (const note of rows) {
    const key = note.agent_id ?? `name:${note.agent_name}`;
    if (seen.has(key)) earlier.set(key, [...(earlier.get(key) ?? []), note]);
    else {
      seen.add(key);
      latest.push(note);
    }
  }

  const card = (note: MeetingNote) => {
    const agent = active.find((a) => a.id === note.agent_id);
    return (
      <NoteCard
        key={note.id}
        note={note}
        workspaceId={workspaceId}
        recordingId={recordingId}
        canEdit={canCreate}
        canExport={canExport}
        canRunAgain={
          canCreate && isAvailable && agent !== undefined && !create.isPending
        }
        isOutdated={
          displayRevisionId !== null &&
          note.revision_id !== null &&
          note.revision_id !== displayRevisionId
        }
        onRunAgain={() => agent && run(agent.id)}
      />
    );
  };

  return (
    <div className="transcript-document flex flex-col gap-5">
      {canCreate && isAvailable && (
        <section aria-labelledby="notes-create-heading">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 id="notes-create-heading" className="eyebrow">
              {t("panel.create")}
            </h3>
            <Button asChild variant="link" size="sm">
              <Link to="/agents">{t("panel.manageAgents")}</Link>
            </Button>
          </div>
          <div className="mt-2 flex flex-col gap-2">
            {agents.isLoading && (
              <Spinner aria-label={t("panel.loadingAgents")} />
            )}
            {groups.map((group) => (
              <div
                key={group.key}
                className="flex flex-wrap items-center gap-2"
              >
                {groups.length > 1 && (
                  <span className="w-full text-xs text-muted-foreground sm:w-auto">
                    {t(`panel.groups.${group.key}`)}
                  </span>
                )}
                {group.agents.map((agent) => (
                  <Button
                    key={agent.id}
                    variant="outline"
                    size="sm"
                    className="rounded-full"
                    title={agent.description ?? undefined}
                    disabled={create.isPending || busyAgents.has(agent.id)}
                    onClick={() => run(agent.id)}
                  >
                    {busyAgents.has(agent.id) && (
                      <Spinner
                        data-icon="inline-start"
                        role="presentation"
                        aria-label={undefined}
                      />
                    )}
                    {agent.name}
                  </Button>
                ))}
              </div>
            ))}
            {!agents.isLoading && groups.length === 0 && (
              <p className="text-sm text-muted-foreground">
                {t("panel.noAgents")}
              </p>
            )}
          </div>
        </section>
      )}
      {!isAvailable && (
        <Alert
          variant="info"
          isInline={true}
          title={t("panel.unavailableTitle")}
        >
          {t("panel.unavailableBody")}
        </Alert>
      )}
      {startError && (
        <Alert
          variant="danger"
          isInline={true}
          isLiveRegion={true}
          title={startError}
          actionClose={
            <Button
              aria-label={t("common:actions.close")}
              variant="ghost"
              size="icon-xs"
              onClick={() => setStartError(null)}
            >
              <XIcon />
            </Button>
          }
        />
      )}
      <DataState
        isLoading={notes.isLoading}
        error={notes.error}
        onRetry={() => void notes.refetch()}
        isEmpty={rows.length === 0}
        emptyTitle={t("panel.emptyTitle")}
        emptyBody={
          canCreate && isAvailable
            ? t("panel.emptyBody")
            : t("panel.emptyBodyReadOnly")
        }
        emptyIcon={NotebookPenIcon}
        headingLevel="h3"
      >
        <div className="flex flex-col gap-4">
          {latest.map((note) => {
            const older =
              earlier.get(note.agent_id ?? `name:${note.agent_name}`) ?? [];
            return (
              <div key={note.id} className="flex flex-col gap-2">
                {card(note)}
                {older.length > 0 && (
                  <Collapsible>
                    <CollapsibleTrigger asChild>
                      <Button variant="link" size="sm" className="self-start">
                        {t("panel.earlier", { count: older.length })}
                      </Button>
                    </CollapsibleTrigger>
                    <CollapsibleContent className="mt-2 flex flex-col gap-3">
                      {older.map(card)}
                    </CollapsibleContent>
                  </Collapsible>
                )}
              </div>
            );
          })}
        </div>
      </DataState>
    </div>
  );
}
