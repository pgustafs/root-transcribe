/**
 * Study notes: the notebooks of a workspace, and one notebook's entries.
 *
 * A notebook is read top to bottom in capture order. Each entry shows its
 * image as soon as it is stored, and says in words what state its text is in.
 */

import axios from "axios";
import { BookOpenIcon, DownloadIcon, ExternalLinkIcon } from "lucide-react";
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
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import {
  useDeleteNote,
  useDeleteNotebook,
  useNotebook,
  useNotebooks,
  useNoteImage,
  useRetryNote,
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
          <ul className="flex flex-col gap-2">
            {rows.map((notebook) => (
              <li key={notebook.id}>
                <RouterLink
                  to={`/workspaces/${workspaceId}/study/${notebook.id}`}
                  className="glass-card flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4 outline-none hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50"
                >
                  <span className="min-w-0">
                    <span className="block truncate font-heading font-medium">
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
        className="flex w-full items-center justify-center rounded-md border bg-muted p-3 text-center text-xs text-muted-foreground sm:w-48"
        style={{ aspectRatio: ratio }}
      >
        {t("page.imageMissing")}
      </div>
    );
  }
  return (
    <div className="flex w-full shrink-0 flex-col gap-2 sm:w-48">
      {url ? (
        <img
          src={url}
          alt={alt}
          className="h-auto w-full rounded-md border"
          style={{ aspectRatio: ratio }}
        />
      ) : (
        <Skeleton
          className="w-full rounded-md"
          style={{ aspectRatio: ratio }}
        />
      )}
      <div>
        <Button
          variant="outline"
          size="sm"
          disabled={!url}
          onClick={() => setOpen(true)}
        >
          {t("page.viewImage")}
        </Button>
      </div>
      {open && url && (
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
    </div>
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
  const [confirming, setConfirming] = useState(false);
  const at = noteClock(note.captured_ms);
  const working = note.status === "pending" || note.status === "processing";
  const headingId = `note-${note.id}`;

  return (
    <li>
      <article
        aria-labelledby={headingId}
        className="flex flex-col gap-4 border-t pt-5 sm:flex-row"
      >
        <NoteImage
          workspaceId={workspaceId}
          note={note}
          alt={t("study:page.imageAlt", { title: notebookTitle, time: at })}
        />
        <div className="flex min-w-0 flex-1 flex-col gap-2">
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
          {/* The state is always words, never colour alone. */}
          <p
            className="flex flex-wrap items-center gap-2 text-sm"
            role={working ? "status" : undefined}
          >
            {working && <Spinner aria-hidden="true" />}
            <Badge variant={note.status === "ready" ? "secondary" : "outline"}>
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
          {note.status === "ready" && note.text && (
            <blockquote className="app-copy border-l-2 pl-3 whitespace-pre-wrap">
              {note.text}
            </blockquote>
          )}
          {note.end_reason && (
            <p className="text-xs text-muted-foreground">
              {t(`study:page.endReason.${note.end_reason}`, {
                defaultValue: "",
              })}
            </p>
          )}
          {note.source_time_url ? (
            <p className="text-sm">
              <a
                href={note.source_time_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 underline underline-offset-4"
              >
                {t("study:page.sourceTimeOpen")}
                <ExternalLinkIcon className="size-3.5" aria-hidden="true" />
              </a>
            </p>
          ) : (
            canLinkSource && (
              <details className="text-sm">
                <summary className="cursor-pointer text-muted-foreground">
                  {t("study:page.sourceTime")}
                </summary>
                <form
                  className="mt-2 flex flex-wrap items-center gap-2"
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (link.trim())
                      setSourceTime.mutate({
                        noteId: note.id,
                        url: link.trim(),
                      });
                  }}
                >
                  <Input
                    type="url"
                    className="max-w-sm"
                    value={link}
                    aria-label={t("study:page.sourceTime")}
                    placeholder={t("study:page.sourceTimeHelp")}
                    aria-invalid={setSourceTime.isError || undefined}
                    onChange={(event) => setLink(event.currentTarget.value)}
                  />
                  <Button type="submit" variant="outline" size="sm">
                    {t("study:page.sourceTimeSave")}
                  </Button>
                  {setSourceTime.isError && (
                    <span role="alert" className="text-sm">
                      {t("study:page.sourceTimeInvalid")}
                    </span>
                  )}
                </form>
              </details>
            )
          )}
          <div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setConfirming(true)}
            >
              {t("study:page.deleteNote")}
            </Button>
          </div>
        </div>
      </article>
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
      downloadBlob(blob, `${name || "study-notes"}.zip`);
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
                className="break-all underline underline-offset-4"
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
          <div className="flex max-w-4xl flex-col gap-8">
            {groups.map((group, index) => (
              <section key={`${group.session?.id ?? "s"}-${index}`}>
                <h2 className="mb-3 font-heading text-lg font-medium">
                  {t("study:page.session", {
                    date: group.session
                      ? day.format(new Date(group.session.started_at))
                      : "",
                  })}
                </h2>
                <ol className="flex flex-col gap-5">
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
