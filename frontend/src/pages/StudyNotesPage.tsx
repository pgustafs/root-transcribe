/**
 * Notebooks: the notebooks of a workspace, and one notebook's entries.
 *
 * A notebook is read top to bottom in capture order. Each entry shows its
 * image as soon as it is stored, and says in words what state its text is in.
 */

import axios from "axios";
import {
  BookOpenIcon,
  ChevronRightIcon,
  DownloadIcon,
  ExternalLinkIcon,
  LinkIcon,
  Maximize2Icon,
  MoreVerticalIcon,
  Trash2Icon,
} from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link as RouterLink, useNavigate, useParams } from "react-router-dom";

import * as studyApi from "@/api/study";
import type { StudyNote, StudySession } from "@/api/study";
import { Alert } from "@/components/Alert";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { DataState } from "@/components/DataState";
import { PageHeader } from "@/components/PageHeader";
import { useRecordingContext } from "@/components/recording/RecordingProvider";
import { Badge } from "@/components/ui/badge";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import {
  useDeleteNote,
  useDeleteNotebook,
  useNotebook,
  useNotebooks,
  useNoteImage,
  useRetryNote,
  useSetNoteComment,
  useSetNoteSourceTime,
} from "@/hooks/useStudy";
import { useWorkspace } from "@/hooks/useWorkspaces";
import { downloadBlob } from "@/utils/downloadBlob";
import { noteClock } from "@/utils/formatDuration";

function Crumbs({
  workspaceId,
  workspaceName,
  notebookTitle,
}: {
  workspaceId: string;
  workspaceName: string;
  notebookTitle?: string;
}) {
  const { t } = useTranslation(["study", "common"]);
  return (
    <Breadcrumb aria-label={t("common:breadcrumb")}>
      <BreadcrumbList>
        <BreadcrumbItem>
          <BreadcrumbLink asChild>
            <RouterLink to={`/workspaces/${workspaceId}`}>
              {workspaceName}
            </RouterLink>
          </BreadcrumbLink>
        </BreadcrumbItem>
        <BreadcrumbSeparator />
        <BreadcrumbItem>
          {notebookTitle ? (
            <BreadcrumbLink asChild>
              <RouterLink to={`/workspaces/${workspaceId}/study`}>
                {t("study:page.notebooks")}
              </RouterLink>
            </BreadcrumbLink>
          ) : (
            <BreadcrumbPage>{t("study:page.notebooks")}</BreadcrumbPage>
          )}
        </BreadcrumbItem>
        {notebookTitle && (
          <>
            <BreadcrumbSeparator />
            <BreadcrumbItem>
              <BreadcrumbPage>{notebookTitle}</BreadcrumbPage>
            </BreadcrumbItem>
          </>
        )}
      </BreadcrumbList>
    </Breadcrumb>
  );
}

function NotebookList({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation(["study", "common"]);
  const workspace = useWorkspace(workspaceId);
  const notebooks = useNotebooks(workspaceId);
  const rows = notebooks.data ?? [];
  return (
    <>
      <PageHeader
        title={t("study:page.notebooks")}
        description={t("study:page.notebooksIntro")}
        breadcrumb={
          <Crumbs
            workspaceId={workspaceId}
            workspaceName={workspace.data?.name ?? ""}
          />
        }
      />
      <section className="page-section">
        <DataState
          isLoading={notebooks.isLoading}
          error={notebooks.error}
          isEmpty={rows.length === 0}
          emptyTitle={t("study:page.empty")}
          emptyBody={t("study:page.emptyHelp")}
          emptyIcon={BookOpenIcon}
          onRetry={() => void notebooks.refetch()}
        >
          <ul className="flex max-w-3xl flex-col gap-2">
            {rows.map((notebook) => (
              <li key={notebook.id}>
                <RouterLink
                  to={`/workspaces/${workspaceId}/study/${notebook.id}`}
                  className="glass-card group flex items-center gap-3 rounded-xl border p-4 outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                >
                  <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-foreground/5">
                    <BookOpenIcon className="size-[18px]" aria-hidden="true" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-heading font-medium group-hover:underline">
                      {notebook.title}
                    </span>
                    {notebook.source_url && (
                      <span className="block truncate text-sm text-muted-foreground">
                        {notebook.source_url}
                      </span>
                    )}
                  </span>
                  <Badge variant="secondary">
                    {t("study:page.notes", { count: notebook.note_count })}
                  </Badge>
                  <ChevronRightIcon
                    className="size-4 shrink-0 text-muted-foreground"
                    aria-hidden="true"
                  />
                </RouterLink>
              </li>
            ))}
          </ul>
        </DataState>
      </section>
    </>
  );
}

function NoteImage({
  workspaceId,
  note,
  alt,
}: {
  workspaceId: string;
  note: StudyNote;
  alt: string;
}) {
  const { t } = useTranslation("study");
  const { url, failed } = useNoteImage(workspaceId, note.id, note.has_image);
  const [open, setOpen] = useState(false);
  const ratio =
    note.image_width && note.image_height
      ? `${note.image_width} / ${note.image_height}`
      : "16 / 9";
  if (!note.has_image || failed) {
    return (
      <div
        className="flex w-full shrink-0 items-center justify-center rounded-md border bg-muted p-3 text-center text-xs text-muted-foreground sm:w-56"
        style={{ aspectRatio: ratio }}
      >
        {t("page.imageMissing")}
      </div>
    );
  }
  if (!url) {
    return (
      <Skeleton
        className="w-full shrink-0 rounded-md sm:w-56"
        style={{ aspectRatio: ratio }}
      />
    );
  }
  return (
    <>
      {/* The picture itself opens the full image. */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={t("page.openImage", { alt })}
        className="group relative w-full shrink-0 cursor-zoom-in self-start overflow-hidden rounded-md border outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:w-56"
      >
        <img
          src={url}
          alt=""
          className="block h-auto w-full"
          style={{ aspectRatio: ratio }}
        />
        <span className="glass-card absolute right-1.5 bottom-1.5 flex size-7 items-center justify-center rounded-md border opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
          <Maximize2Icon className="size-3.5" aria-hidden="true" />
        </span>
      </button>
      {open && (
        <Dialog open onOpenChange={(next) => !next && setOpen(false)}>
          <DialogContent
            aria-describedby={undefined}
            className="max-h-[95dvh] overflow-auto sm:max-w-[95vw]"
          >
            <DialogHeader>
              <DialogTitle>{alt}</DialogTitle>
            </DialogHeader>
            <img src={url} alt={alt} className="h-auto max-w-none" />
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

/** The owner's own words on a note. Saved when the field is left. */
function NoteThoughts({
  workspaceId,
  notebookId,
  note,
}: {
  workspaceId: string;
  notebookId: string;
  note: StudyNote;
}) {
  const { t } = useTranslation("study");
  const save = useSetNoteComment(workspaceId, notebookId);
  const stored = note.comment ?? "";
  const [value, setValue] = useState(stored);
  const [savedOnce, setSavedOnce] = useState(false);
  const id = `note-thoughts-${note.id}`;
  const commit = () => {
    if (value.trim() === stored.trim()) return;
    save.mutate(
      { noteId: note.id, comment: value },
      { onSuccess: () => setSavedOnce(true) },
    );
  };
  return (
    <Field data-invalid={save.isError || undefined}>
      <FieldLabel htmlFor={id}>{t("page.thoughts")}</FieldLabel>
      <Textarea
        id={id}
        value={value}
        maxLength={10000}
        placeholder={t("page.thoughtsPlaceholder")}
        aria-invalid={save.isError || undefined}
        aria-describedby={`${id}-state`}
        onChange={(event) => {
          setValue(event.currentTarget.value);
          setSavedOnce(false);
        }}
        onBlur={commit}
      />
      <FieldDescription id={`${id}-state`} aria-live="polite">
        {save.isError
          ? t("page.thoughtsFailed")
          : save.isPending
            ? t("page.thoughtsSaving")
            : savedOnce
              ? t("page.thoughtsSaved")
              : t("page.thoughtsHelp")}
      </FieldDescription>
    </Field>
  );
}

function NoteEntry({
  workspaceId,
  notebookId,
  notebookTitle,
  canLinkSource,
  note,
}: {
  workspaceId: string;
  notebookId: string;
  notebookTitle: string;
  canLinkSource: boolean;
  note: StudyNote;
}) {
  const { t } = useTranslation(["study", "common"]);
  const retry = useRetryNote(workspaceId, notebookId);
  const setSourceTime = useSetNoteSourceTime(workspaceId, notebookId);
  const remove = useDeleteNote(workspaceId, notebookId);
  const [link, setLink] = useState("");
  const [linking, setLinking] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const at = noteClock(note.captured_ms);
  const working = note.status === "pending" || note.status === "processing";
  const hasText = note.status === "ready" && Boolean(note.text);
  const headingId = `note-${note.id}`;
  const linkId = `note-link-${note.id}`;

  return (
    <li>
      <Card
        role="article"
        aria-labelledby={headingId}
        className="glass-card gap-0 p-4 shadow-none sm:flex-row sm:gap-5"
      >
        <NoteImage
          workspaceId={workspaceId}
          note={note}
          alt={t("study:page.imageAlt", { title: notebookTitle, time: at })}
        />
        <div className="mt-4 flex min-w-0 flex-1 flex-col gap-4 sm:mt-0">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <h3 id={headingId} className="font-heading text-base font-medium">
                {t("study:page.at", { time: at })}
              </h3>
              {note.audio_start_ms !== null && note.audio_end_ms !== null && (
                <p className="text-sm text-muted-foreground">
                  {t("study:page.audio", {
                    start: noteClock(note.audio_start_ms),
                    end: noteClock(note.audio_end_ms),
                  })}
                </p>
              )}
            </div>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t("study:page.actions", { time: at })}
                >
                  <MoreVerticalIcon data-icon="inline-start" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuGroup>
                  {canLinkSource && !note.source_time_url && (
                    <DropdownMenuItem onSelect={() => setLinking(true)}>
                      <LinkIcon />
                      {t("study:page.sourceTime")}
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem onSelect={() => setConfirming(true)}>
                    <Trash2Icon />
                    {t("study:page.deleteNote")}
                  </DropdownMenuItem>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>

          <div className="flex flex-col gap-2">
            <h4 className="eyebrow px-0">{t("study:page.transcript")}</h4>
            {hasText ? (
              <blockquote className="app-copy border-l-2 border-l-accent-line pl-3 whitespace-pre-wrap text-body">
                {note.text}
              </blockquote>
            ) : (
              // The state is always words, never colour alone.
              <p
                className="flex flex-wrap items-center gap-2 text-sm"
                role={working ? "status" : undefined}
              >
                {working && <Spinner aria-hidden="true" />}
                <Badge variant="outline">
                  {t(`study:page.status.${note.status}`)}
                </Badge>
                {note.status === "failed" && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={retry.isPending}
                    onClick={() => retry.mutate(note.id)}
                  >
                    {t("study:page.retry")}
                  </Button>
                )}
              </p>
            )}
            {note.end_reason && (
              <p className="text-xs text-muted-foreground">
                {t(`study:page.endReason.${note.end_reason}`, {
                  defaultValue: "",
                })}
              </p>
            )}
            {note.source_time_url && (
              <p className="text-sm">
                <a
                  href={note.source_time_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex items-center gap-1 text-link underline underline-offset-4"
                >
                  {t("study:page.sourceTimeOpen")}
                  <ExternalLinkIcon className="size-3.5" aria-hidden="true" />
                </a>
              </p>
            )}
          </div>

          {linking && !note.source_time_url && (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (link.trim())
                  setSourceTime.mutate(
                    { noteId: note.id, url: link.trim() },
                    { onSuccess: () => setLinking(false) },
                  );
              }}
            >
              <Field data-invalid={setSourceTime.isError || undefined}>
                <FieldLabel htmlFor={linkId}>
                  {t("study:page.sourceTime")}
                </FieldLabel>
                <div className="flex flex-wrap items-center gap-2">
                  <Input
                    id={linkId}
                    type="url"
                    className="max-w-sm"
                    value={link}
                    aria-invalid={setSourceTime.isError || undefined}
                    aria-describedby={`${linkId}-help`}
                    onChange={(event) => setLink(event.currentTarget.value)}
                  />
                  <Button type="submit" variant="outline" size="sm">
                    {t("study:page.sourceTimeSave")}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setLinking(false)}
                  >
                    {t("common:cancel")}
                  </Button>
                </div>
                <FieldDescription id={`${linkId}-help`} aria-live="polite">
                  {setSourceTime.isError
                    ? t("study:page.sourceTimeInvalid")
                    : t("study:page.sourceTimeHelp")}
                </FieldDescription>
              </Field>
            </form>
          )}

          <Separator />
          <NoteThoughts
            workspaceId={workspaceId}
            notebookId={notebookId}
            note={note}
          />
        </div>
      </Card>
      {confirming && (
        <ConfirmDeleteModal
          isOpen
          itemName={t("study:page.at", { time: at })}
          isDeleting={remove.isPending}
          onConfirm={() =>
            remove.mutate(note.id, { onSettled: () => setConfirming(false) })
          }
          onClose={() => setConfirming(false)}
        />
      )}
    </li>
  );
}

function NotebookView({
  workspaceId,
  notebookId,
}: {
  workspaceId: string;
  notebookId: string;
}) {
  const { t, i18n } = useTranslation(["study", "common"]);
  const navigate = useNavigate();
  const workspace = useWorkspace(workspaceId);
  const recording = useRecordingContext();
  // While the recorder is adding notes here, keep the page up to date.
  const live =
    recording.study.active && recording.study.notebookId === notebookId;
  const notebook = useNotebook(workspaceId, notebookId, live);
  const removeNotebook = useDeleteNotebook(workspaceId);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const data = notebook.data;

  const exportMarkdown = async () => {
    if (!data) return;
    setExporting(true);
    setExportError(null);
    try {
      const blob = await studyApi.exportNotebook(workspaceId, notebookId);
      const name = data.title
        .replace(/[^\p{L}\p{N}]+/gu, "-")
        .replace(/^-|-$/g, "");
      downloadBlob(blob, `${name || "notebook"}.zip`);
    } catch (error) {
      setExportError(
        axios.isAxiosError(error) && error.response?.status === 403
          ? t("study:page.exportDenied")
          : t("study:page.exportFailed"),
      );
    } finally {
      setExporting(false);
    }
  };

  const sessions = new Map<string, StudySession>(
    (data?.sessions ?? []).map((session) => [session.id, session]),
  );
  // Group the (already ordered) notes under their session heading.
  const groups: { session: StudySession | undefined; notes: StudyNote[] }[] =
    [];
  for (const note of data?.notes ?? []) {
    const last = groups[groups.length - 1];
    if (last && last.session?.id === note.session_id) last.notes.push(note);
    else groups.push({ session: sessions.get(note.session_id), notes: [note] });
  }
  const day = new Intl.DateTimeFormat(i18n.language, { dateStyle: "long" });

  return (
    <>
      <PageHeader
        title={data?.title ?? t("study:title")}
        breadcrumb={
          <Crumbs
            workspaceId={workspaceId}
            workspaceName={workspace.data?.name ?? ""}
            notebookTitle={data?.title ?? "…"}
          />
        }
        labels={
          data && (
            <Badge variant="secondary">
              {t("study:page.notes", { count: data.notes.length })}
            </Badge>
          )
        }
        description={
          data?.source_url ? (
            <>
              {t("study:page.source")}:{" "}
              <a
                href={data.source_url}
                target="_blank"
                rel="noreferrer noopener"
                className="break-all text-link underline underline-offset-4"
              >
                {data.source_url}
              </a>
            </>
          ) : undefined
        }
        actions={
          data && (
            <div className="flex flex-wrap items-center gap-2">
              <Button
                onClick={() => void exportMarkdown()}
                disabled={exporting || data.notes.length === 0}
                aria-busy={exporting}
              >
                {exporting ? (
                  <Spinner data-icon="inline-start" aria-hidden="true" />
                ) : (
                  <DownloadIcon data-icon="inline-start" />
                )}
                {exporting ? t("study:page.exporting") : t("study:page.export")}
              </Button>
              <Button variant="ghost" onClick={() => setConfirming(true)}>
                {t("study:page.delete")}
              </Button>
            </div>
          )
        }
      />
      <section className="page-section">
        <div aria-live="polite">
          {exportError && (
            <Alert
              variant="danger"
              isInline
              title={exportError}
              className="mb-4"
            />
          )}
        </div>
        <DataState
          isLoading={notebook.isLoading}
          error={notebook.error}
          isEmpty={(data?.notes.length ?? 0) === 0}
          emptyTitle={t("study:page.empty")}
          emptyBody={t("study:page.emptyHelp")}
          emptyIcon={BookOpenIcon}
          onRetry={() => void notebook.refetch()}
        >
          <div className="mx-auto flex max-w-3xl flex-col gap-8">
            {groups.map((group, index) => (
              <section key={`${group.session?.id ?? "s"}-${index}`}>
                <h2 className="eyebrow mb-3 px-0">
                  {t("study:page.session", {
                    date: group.session
                      ? day.format(new Date(group.session.started_at))
                      : "",
                  })}
                </h2>
                <ol className="flex flex-col gap-3">
                  {group.notes.map((note) => (
                    <NoteEntry
                      key={note.id}
                      workspaceId={workspaceId}
                      notebookId={notebookId}
                      notebookTitle={data?.title ?? ""}
                      canLinkSource={Boolean(data?.source_key)}
                      note={note}
                    />
                  ))}
                </ol>
              </section>
            ))}
          </div>
        </DataState>
      </section>
      {confirming && data && (
        <ConfirmDeleteModal
          isOpen
          itemName={data.title}
          consequence={t("study:page.deleteConfirm", { title: data.title })}
          isDeleting={removeNotebook.isPending}
          onConfirm={() =>
            removeNotebook.mutate(notebookId, {
              onSuccess: () => navigate(`/workspaces/${workspaceId}/study`),
              onSettled: () => setConfirming(false),
            })
          }
          onClose={() => setConfirming(false)}
        />
      )}
    </>
  );
}

export function StudyNotesPage() {
  const { id = "", notebookId } = useParams<{
    id: string;
    notebookId?: string;
  }>();
  return notebookId ? (
    <NotebookView workspaceId={id} notebookId={notebookId} />
  ) : (
    <NotebookList workspaceId={id} />
  );
}
