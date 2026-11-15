import type { RecordingLanguage } from "@/api/types";
import { RecordingLanguageField } from "@/components/recording/RecordingLanguageField";
import { FieldGroup } from "@/components/ui/field";
import {
  MicIcon as MicrophoneIcon,
  UploadIcon,
  XIcon,
  ChevronDownIcon,
  DownloadIcon,
  MoreHorizontalIcon,
  RotateCcwIcon,
  RotateCwIcon,
  UsersIcon,
  PencilIcon,
} from "lucide-react";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from "@/components/ui/collapsible";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import {
  DialogHeader,
  DialogTitle,
  Dialog,
  DialogContent,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  EmptyDescription,
  Empty,
  EmptyHeader,
  EmptyTitle,
  EmptyMedia,
} from "@/components/ui/empty";
import { cn } from "@/lib/utils";
import { Checkbox } from "@/components/ui/checkbox";
import { Field, FieldLabel } from "@/components/ui/field";
import {
  DropdownMenuItem,
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
} from "@/components/ui/dropdown-menu";
import {
  NativeSelectOption,
  NativeSelect,
} from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { WorkspaceMembersTab } from "@/components/workspace/WorkspaceMembersTab";
/** Recording library and speaker-attributed transcript review. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams, useSearchParams } from "react-router-dom";

import { getErrorMessage } from "@/api/client";
import {
  createPlaybackSession,
  downloadRecordingAudio,
  exportRevision,
  reportClientEvent,
  setRecordingSensitivity,
} from "@/api/meetings";
import type {
  MeetingRecording,
  MeetingSegment,
  MeetingStatus,
} from "@/api/types";
import { Alert } from "@/components/Alert";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { DataState } from "@/components/DataState";
import { PageHeader } from "@/components/PageHeader";
import { PREFLIGHT_EVENTS } from "@/components/recording/RecordingProvider";
import { Link as RouterLink } from "react-router-dom";
import {
  Breadcrumb,
  BreadcrumbList,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbSeparator,
  BreadcrumbPage,
} from "@/components/ui/breadcrumb";
import { RowActions } from "@/components/RowActions";
import { SearchInput } from "@/components/SearchInput";
import { StatusLabel } from "@/components/StatusLabel";
import { useToast } from "@/components/toast/useToast";
import { browserInfo, mediaTokens } from "@/lib/clientDiagnostics";
import {
  danger,
  MEETING_STATUS,
  neutral,
  type StatusPresentation,
  tag,
} from "@/lib/labels";
import {
  isRetryableMeetingError,
  meetingErrorMessage,
} from "@/lib/meetingErrors";
import {
  type PreflightReason,
  preflightRecording,
} from "@/lib/recordingPreflight";
import { downloadBlob } from "@/utils/downloadBlob";

import { MeetingClipsPanel } from "@/components/workspace/MeetingClipsPanel";
import { MeetingNotesPanel } from "@/components/workspace/MeetingNotesPanel";
import { MeetingCommentsDrawer } from "@/components/workspace/MeetingCommentsDrawer";
import { MeetingDlpPanel } from "@/components/workspace/MeetingDlpPanel";
import { MeetingRevisionsModal } from "@/components/workspace/MeetingRevisionsModal";
import { RecordMeetingModal } from "@/components/workspace/RecordMeetingModal";
import { SpeakerFilter } from "@/components/workspace/SpeakerFilter";
import { RecordingGuide } from "@/components/RecordingGuide";
import { speakerLabel } from "@/utils/speakerLabel";
import { useConfig } from "@/hooks/useConfig";
import {
  meetingKeys,
  useAcceptSpeakerSuggestion,
  useAddManualDlpFinding,
  useAssignSpeakerByLabel,
  useCreateRecording,
  useDeleteRecording,
  useMeetingPermissions,
  useRecording,
  useRecordings,
  useRejectSpeakerSuggestion,
  useRetryRecording,
  useRevisions,
  useSpeakerSuggestions,
  useUpdateSegment,
} from "@/hooks/useMeetings";
import { useSpeakers } from "@/hooks/useSpeakers";
import { useWorkspace } from "@/hooks/useWorkspaces";
import { permissionsFor } from "@/utils/workspacePermissions";

/** Localized message per client-preflight reason for the file-picker upload
 * path (issue #37) — mirrors the recorder's mapping. */
const PICKER_PREFLIGHT_KEYS: Record<PreflightReason, string> = {
  empty: "recordingBar.mediaEmpty",
  too_small: "recordingBar.mediaTooSmall",
  bad_header: "recordingBar.mediaInvalid",
};

const TERMINAL: MeetingStatus[] = ["completed", "failed"];
const AUDIO_ACCEPT = ".webm,.ogg,.opus,.mp3,.mp4,.m4a,.wav,.flac,.aac";

function fmtSeconds(sec: number | null): string {
  if (!sec) return "";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

const EXPORT_FORMATS = ["md", "txt", "srt", "vtt", "docx", "pdf"] as const;

/** Horizontal speaker lane (issue #6, C4): one block per segment, colored by
 * speaker, clickable to seek, with a playhead line. SVG geometry attributes
 * carry the dynamic widths — no inline styles. */
function MeetingTimeline({
  segments,
  currentMs,
  onSeek,
}: {
  segments: MeetingSegment[];
  currentMs: number;
  onSeek: (ms: number) => void;
}) {
  const { t } = useTranslation(["meetings"]);
  const totalMs = Math.max(...segments.map((s) => s.end_ms), 1);
  const speakerOrder = [...new Set(segments.map((s) => s.speaker_label))];
  const scale = (ms: number) => (ms / totalMs) * 1000;
  return (
    <svg
      viewBox="0 0 1000 24"
      preserveAspectRatio="none"
      className="meeting-timeline"
      role="group"
      aria-label={t("detail.timelineAria")}
    >
      {segments.map((seg) => (
        <rect
          key={seg.id}
          x={scale(seg.start_ms)}
          y={2}
          width={Math.max(2, scale(seg.end_ms - seg.start_ms))}
          height={20}
          rx={2}
          className={`meeting-lane meeting-lane-${
            speakerOrder.indexOf(seg.speaker_label) % 6
          }`}
          role="button"
          tabIndex={0}
          aria-label={t("detail.timelineSegment", {
            speaker:
              seg.speaker_name ??
              speakerLabel(seg.speaker_label, (number) =>
                t("review.speaker", { number }),
              ),
            ts: fmtTs(seg.start_ms),
          })}
          onClick={() => onSeek(seg.start_ms)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              onSeek(seg.start_ms);
            }
          }}
        >
          <title>{`${seg.speaker_name ?? speakerLabel(seg.speaker_label, (number) => t("review.speaker", { number }))} · ${fmtTs(
            seg.start_ms,
          )}`}</title>
        </rect>
      ))}
      <line
        x1={scale(currentMs)}
        x2={scale(currentMs)}
        y1={0}
        y2={24}
        className="meeting-playhead"
      />
    </svg>
  );
}

const SENSITIVITIES = ["internal", "confidential", "restricted"] as const;
/** Sensitivity is a classification level: the text carries it, the tone
 * escalates with it (never colour alone). */
const SENSITIVITY_LABEL: Record<
  MeetingRecording["sensitivity"],
  StatusPresentation
> = {
  internal: neutral("meetings:detail.sensitivity.internal"),
  confidential: tag("meetings:detail.sensitivity.confidential", "orange"),
  restricted: danger("meetings:detail.sensitivity.restricted"),
};

const LEGAL_HOLD = tag("meetings:detail.legalHold", "purple");
const EDITED_BADGE = neutral("meetings:detail.editedBadge");

function fmtTs(ms: number): string {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function WorkspaceMeetingsPage() {
  const isMobile = useIsMobile();
  const [libraryOpen, setLibraryOpen] = useState(false);
  const { id = "" } = useParams();
  const { t, i18n } = useTranslation(["meetings", "common"]);
  const workspace = useWorkspace(id);
  const perms = permissionsFor(workspace.data?.my_role);
  const canEdit = perms.canEditContent;

  const [deepLinkParams] = useSearchParams();
  // ?recording= opens a specific recording, including older pages.
  const [openId, setOpenId] = useState<string | null>(() =>
    deepLinkParams.get("recording"),
  );
  const [recordOpen, setRecordOpen] = useState(false);
  const [deleting, setDeleting] = useState<MeetingRecording | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const toast = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const config = useConfig();
  const serviceReady =
    config.data?.meetings_status === "available" &&
    config.data.meetings_enabled;
  const meetingPerms = useMeetingPermissions(id);
  const can = (cap: string) => meetingPerms.data?.has(cap) ?? false;
  const recordings = useRecordings(id);
  const retry = useRetryRecording(id);
  const del = useDeleteRecording(id);
  const create = useCreateRecording(id);

  // Auto-open the newest recording so the main pane is never pointlessly empty.
  const rows = useMemo(() => {
    // Dedupe by id: keyset paging makes boundary duplicates unlikely, but a
    // refetch racing a mutation can still repeat a row — and duplicate React
    // keys corrupt the list.
    const seen = new Set<string>();
    return (recordings.data?.pages.flat() ?? []).filter((r) =>
      seen.has(r.id) ? false : (seen.add(r.id), true),
    );
  }, [recordings.data]);
  // A deep-linked recording may live outside the loaded pages — never
  // replace it with the newest row just because the page doesn't hold it.
  const deepLinkedIdRef = useRef(deepLinkParams.get("recording"));
  const listLoaded = !recordings.isPending;
  useEffect(() => {
    // A cold load has NO rows yet (review round 4: the empty-list clause ran
    // while page 1 was still in flight and wiped ?recording= deep links) —
    // selection bookkeeping only makes sense against a loaded list.
    if (!listLoaded) return;
    if (openId === null && rows.length > 0) setOpenId(rows[0].id);
    if (
      openId !== null &&
      openId !== deepLinkedIdRef.current &&
      rows.length > 0 &&
      !rows.some((r) => r.id === openId)
    ) {
      setOpenId(rows[0].id); // selection was deleted
    }
    if (
      rows.length === 0 &&
      openId !== null &&
      openId !== deepLinkedIdRef.current
    )
      setOpenId(null);
  }, [rows, openId, listLoaded]);

  // Uploaded files need the same attestation as live recordings when the
  // deployment requires it (issue #6): the pre-#6 path hardcoded consent=false
  // with no way to assert it.
  const [attestFile, setAttestFile] = useState<File | null>(null);
  const [attestConsent, setAttestConsent] = useState(false);
  const [uploadLanguage, setUploadLanguage] =
    useState<RecordingLanguage>("auto");
  const consentRequired = config.data?.meeting_consent_required ?? false;

  const uploadFile = async (file: File, consent: boolean) => {
    setUploadError(null);
    if (file.size > (config.data?.audio_max_bytes ?? 500 * 1024 * 1024)) {
      setUploadError(
        t("page.fileTooLarge", {
          size: Math.floor(
            (config.data?.audio_max_bytes ?? 500 * 1024 * 1024) / 1024 / 1024,
          ),
        }),
      );
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    // The picker path gets the SAME client preflight as a live recording
    // (issue #37): a malformed file otherwise uploads fine at the transport
    // layer and only fails asynchronously in the worker.
    // Pass the filename too: pickers often report an empty or generic MIME, and
    // without it a malformed .webm would skip header validation (issue #37).
    const check = await preflightRecording(file, file.type, file.name);
    if (!check.ok) {
      setUploadError(t(PICKER_PREFLIGHT_KEYS[check.reason]));
      // Operators see client-side rejections too (issue #37) — this file never
      // reaches the worker, so the server has no other signal. Best-effort.
      const { family, major } = browserInfo();
      void reportClientEvent(id, {
        event: PREFLIGHT_EVENTS[check.reason],
        browser_family: family,
        browser_major: major,
        ...mediaTokens(file.type, file.name),
      });
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    try {
      const { family, major } = browserInfo();
      const rec = await create.mutateAsync({
        file,
        filename: file.name,
        title: file.name.replace(/\.[^.]+$/, ""),
        consent,
        requestedLanguage: uploadLanguage,
        // A picker upload the worker later rejects still carries attribution
        // (issue #37 r8) — closed tokens only, same as the recorder path.
        clientDiagnostics: {
          browser_family: family,
          browser_major: major,
          ...mediaTokens(file.type, file.name),
          total_bytes: file.size,
        },
      });
      setOpenId(rec.id);
      toast.success(t("toast.uploaded", { name: file.name }));
    } catch (e) {
      setUploadError(getErrorMessage(e, t("errors.upload")));
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const onFilePicked = async (file: File | null) => {
    if (!file) return;
    setAttestConsent(false);
    setAttestFile(file);
  };

  const dateFmt = new Intl.DateTimeFormat(i18n.language, {
    dateStyle: "medium",
    timeStyle: "short",
  });

  const retryRecording = (r: MeetingRecording) => {
    setActionError(null);
    retry.mutate(r.id, {
      onSuccess: () => toast.success(t("toast.retried", { name: r.title })),
      onError: (error) =>
        setActionError(getErrorMessage(error, t("errors.retry"))),
    });
  };

  const confirmDelete = () => {
    if (!deleting) return;
    const { id: recordingId, title } = deleting;
    setActionError(null);
    del.mutate(recordingId, {
      onSuccess: () => {
        setDeleting(null);
        toast.success(t("toast.deleted", { name: title }));
      },
      onError: (error) => {
        setDeleting(null);
        setActionError(getErrorMessage(error, t("errors.delete")));
      },
    });
  };

  const [membersOpen, setMembersOpen] = useState(false);
  const headerActions = canEdit ? (
    <div className="flex flex-wrap items-center gap-2">
      <div>
        <Button
          onClick={() => setRecordOpen(true)}
          disabled={!serviceReady || !can("record")}
          variant="default"
        >
          {<MicrophoneIcon data-icon="inline-start" />}
          {t("record")}
        </Button>
      </div>
      <div>
        <Button
          onClick={() => fileInputRef.current?.click()}
          disabled={!serviceReady || create.isPending || !can("upload")}
          aria-busy={create.isPending}
          variant="outline"
        >
          {<UploadIcon data-icon="inline-start" />}
          {create.isPending && (
            <Spinner data-icon="inline-start" aria-label={undefined} />
          )}
          {t("page.uploadAudio")}
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          accept={AUDIO_ACCEPT}
          className="visually-hidden-file-input"
          aria-label={t("page.uploadAudio")}
          onChange={(e) => onFilePicked(e.target.files?.[0] ?? null)}
        />
      </div>
      {perms.canManageMembers && (
        <div>
          <Button onClick={() => setMembersOpen(true)} variant="ghost">
            <UsersIcon data-icon="inline-start" />
            {t("access:workspaces.members")}
          </Button>
        </div>
      )}
    </div>
  ) : undefined;

  return (
    <>
      {membersOpen && (
        <Dialog
          open={true}
          onOpenChange={(open) => {
            if (!open) (() => setMembersOpen(false))();
          }}
        >
          <DialogContent
            aria-labelledby="workspace-members-title"
            aria-describedby={undefined}
            className="max-h-[90dvh] overflow-y-auto sm:max-w-5xl"
          >
            <DialogHeader>
              <DialogTitle id="workspace-members-title">
                {t("access:workspaces.members")}
              </DialogTitle>
            </DialogHeader>
            <div className="min-w-0">
              <WorkspaceMembersTab
                workspaceId={id}
                permissions={permissionsFor(workspace.data?.my_role)}
              />
            </div>
          </DialogContent>
        </Dialog>
      )}
      <PageHeader
        title={workspace.data?.name ?? t("title")}
        description={workspace.data?.description || undefined}
        actions={headerActions}
        breadcrumb={
          <Breadcrumb aria-label={t("common:breadcrumb")}>
            <BreadcrumbList>
              <BreadcrumbItem>
                <BreadcrumbLink asChild>
                  <RouterLink to="/workspaces">
                    {t("common:nav.workspaces")}
                  </RouterLink>
                </BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbSeparator />
              <BreadcrumbItem>
                <BreadcrumbPage>
                  {workspace.data?.name ?? t("title")}
                </BreadcrumbPage>
              </BreadcrumbItem>
            </BreadcrumbList>
          </Breadcrumb>
        }
      />
      <section className="page-section flex-1 min-h-0">
        <div className="workspace-editor">
          {!config.isPending && !serviceReady && (
            <div>
              <Alert
                variant="warning"
                isInline={true}
                title={t("page.serviceUnavailable")}
              >
                {t("page.serviceUnavailableBody")}
                <Button onClick={() => void config.refetch()} variant="link">
                  {t("page.checkAgain")}
                </Button>
              </Alert>
            </div>
          )}
          {uploadError && (
            <div>
              <Alert
                variant="danger"
                isInline={true}
                isLiveRegion={true}
                title={uploadError}
                actionClose={
                  <Button
                    aria-label={t("common:actions.close")}
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => setUploadError(null)}
                  >
                    <XIcon data-icon="inline-start" />
                  </Button>
                }
              />
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
          <Card className="workspace-split grid gap-0 py-0">
            <Collapsible
              open={!isMobile || libraryOpen}
              onOpenChange={setLibraryOpen}
              asChild
            >
              <section
                className="recording-library"
                aria-labelledby="meetings-recordings-heading"
              >
                <div className="recording-library-heading">
                  <h2
                    id="meetings-recordings-heading"
                    className="text-sm font-semibold"
                  >
                    {t("page.recordingsPane")}
                  </h2>
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary">
                      {rows.length}
                      {recordings.hasNextPage ? "+" : ""}
                    </Badge>
                    {isMobile && (
                      <CollapsibleTrigger asChild>
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={t("page.chooseRecording")}
                        >
                          {t("page.chooseRecording")}
                          <ChevronDownIcon
                            data-icon="inline-end"
                            className={cn(libraryOpen && "rotate-180")}
                          />
                        </Button>
                      </CollapsibleTrigger>
                    )}
                  </div>
                </div>
                <Separator />
                <CollapsibleContent className="recording-library-list">
                  <DataState
                    isLoading={recordings.isLoading}
                    error={recordings.error}
                    onRetry={() => void recordings.refetch()}
                    isEmpty={rows.length === 0}
                    emptyTitle={t("empty.title")}
                    emptyBody={t("empty.body")}
                    emptyIcon={MicrophoneIcon}
                    loadingLabel={t("page.loading")}
                    headingLevel="h3"
                  >
                    <div className="flex flex-col">
                      {rows.map((r) => (
                        <div
                          key={r.id}
                          className={cn(
                            "meeting-row",
                            r.id === openId && "meeting-row-selected",
                          )}
                        >
                          <button
                            type="button"
                            aria-label={r.title}
                            aria-current={r.id === openId ? "true" : undefined}
                            onClick={() => {
                              setOpenId(r.id);
                              setLibraryOpen(false);
                            }}
                            className="meeting-row-select"
                          >
                            <span className="meeting-row-title">{r.title}</span>
                            <span className="meeting-row-date">
                              {dateFmt.format(new Date(r.created_at))}
                            </span>
                            <span className="meeting-row-meta">
                              <StatusLabel
                                presentation={MEETING_STATUS[r.status]}
                                value={r.status}
                                isCompact
                              />
                              {r.duration_seconds != null && (
                                <span>{fmtSeconds(r.duration_seconds)}</span>
                              )}
                            </span>
                          </button>
                          <div className="meeting-row-actions">
                            <RowActions
                              toggleAriaLabel={t("actionsFor", {
                                name: r.title,
                              })}
                              items={[
                                // Retry is offered for terminal recordings,
                                // but hidden for a NON-retryable media fault
                                // (issue #37) — the backend would 409 it, and
                                // re-running the same object fails identically.
                                ...(canEdit &&
                                TERMINAL.includes(r.status) &&
                                isRetryableMeetingError(r.error_code)
                                  ? [
                                      {
                                        title: t("actions.retry"),
                                        onClick: () => retryRecording(r),
                                      },
                                    ]
                                  : []),
                                ...(canEdit
                                  ? [
                                      {
                                        title: t("actions.delete"),
                                        isDanger: true,
                                        onClick: () => setDeleting(r),
                                      },
                                    ]
                                  : []),
                              ]}
                            />
                          </div>
                        </div>
                      ))}
                      {recordings.hasNextPage && (
                        <div>
                          <Button
                            onClick={() => void recordings.fetchNextPage()}
                            disabled={recordings.isFetchingNextPage}
                            aria-busy={recordings.isFetchingNextPage}
                            variant="link"
                          >
                            {recordings.isFetchingNextPage && (
                              <Spinner
                                data-icon="inline-start"
                                aria-label={undefined}
                              />
                            )}
                            {t("list.showMore")}
                          </Button>
                        </div>
                      )}
                    </div>
                  </DataState>
                </CollapsibleContent>
                <div className="recording-library-help">
                  <Separator />
                  <div className="p-4">
                    <small className="app-copy">
                      {t("page.uploadHelp", {
                        size: Math.floor(
                          (config.data?.audio_max_bytes ?? 500 * 1024 * 1024) /
                            1024 /
                            1024,
                        ),
                        minutes: config.data?.recording_max_minutes ?? 120,
                      })}
                    </small>
                  </div>
                </div>
              </section>
            </Collapsible>
            <Separator orientation="vertical" className="hidden md:block" />
            <section
              className="meetings-detail-pane"
              aria-label={t("page.detailPane")}
            >
              {openId ? (
                <MeetingDetail
                  key={openId}
                  workspaceId={id}
                  recordingId={openId}
                  canEdit={canEdit}
                  isOwner={perms.isOwner}
                />
              ) : (
                <Empty>
                  <EmptyHeader>
                    <EmptyMedia variant="icon">
                      <MicrophoneIcon />
                    </EmptyMedia>
                    <EmptyTitle
                      role="heading"
                      aria-level={Number("h2".slice(1))}
                    >
                      {t("empty.title")}
                    </EmptyTitle>
                  </EmptyHeader>
                  <EmptyDescription>{t("page.selectPrompt")}</EmptyDescription>
                  <RecordingGuide />
                </Empty>
              )}
            </section>
          </Card>
        </div>
      </section>

      {recordOpen && (
        <RecordMeetingModal
          workspaceId={id}
          isOpen={recordOpen}
          onClose={() => setRecordOpen(false)}
          onUploaded={(rid) => setOpenId(rid)}
        />
      )}
      {attestFile && (
        <Dialog
          open={true}
          onOpenChange={(open) => {
            if (!open)
              (() => {
                setAttestFile(null);
                if (fileInputRef.current) fileInputRef.current.value = "";
              })();
          }}
        >
          <DialogContent
            aria-labelledby="upload-attest-title"
            aria-describedby="upload-attest-body"
            className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
          >
            <DialogHeader>
              <DialogTitle id="upload-attest-title">
                {t("page.uploadAudio")}
              </DialogTitle>
            </DialogHeader>
            <div className="min-w-0">
              <FieldGroup>
                <p id="upload-attest-body" className="app-copy break-words">
                  {consentRequired
                    ? t("page.uploadConsentBody", { name: attestFile.name })
                    : t("page.uploadSettingsBody", { name: attestFile.name })}
                </p>
                <RecordingLanguageField
                  value={uploadLanguage}
                  onChange={setUploadLanguage}
                />
                {consentRequired && (
                  <Field orientation="horizontal">
                    <Checkbox
                      id="upload-participant-consent"
                      checked={attestConsent}
                      onCheckedChange={(state) => {
                        const v = state === true;
                        return setAttestConsent(v);
                      }}
                    />
                    <FieldLabel htmlFor="upload-participant-consent">
                      {t("recordModal.consent")}
                    </FieldLabel>
                  </Field>
                )}
              </FieldGroup>
            </div>
            <DialogFooter>
              <Button
                onClick={async () => {
                  const file = attestFile;
                  setAttestFile(null);
                  if (file) await uploadFile(file, attestConsent);
                }}
                disabled={
                  (consentRequired && !attestConsent) || create.isPending
                }
                aria-busy={create.isPending}
                variant="default"
              >
                {create.isPending && (
                  <Spinner data-icon="inline-start" aria-label={undefined} />
                )}
                {t("page.uploadAudio")}
              </Button>
              <Button
                onClick={() => {
                  setAttestFile(null);
                  if (fileInputRef.current) fileInputRef.current.value = "";
                }}
                variant="link"
              >
                {t("common:actions.cancel")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {deleting && (
        <ConfirmDeleteModal
          isOpen={true}
          itemName={deleting.title}
          title={t("delete.title")}
          consequence={t("delete.consequence")}
          isDeleting={del.isPending}
          onConfirm={confirmDelete}
          onClose={() => setDeleting(null)}
        />
      )}
    </>
  );
}

function MeetingDetail({
  workspaceId,
  recordingId,
  canEdit,
  isOwner,
}: {
  workspaceId: string;
  recordingId: string;
  canEdit: boolean;
  /** Workspace owner — may delete anyone's comment (issue #6, D4). */
  isOwner: boolean;
}) {
  const { t } = useTranslation(["meetings", "common", "notes"]);
  const toast = useToast();
  const detail = useRecording(workspaceId, recordingId, true);
  // Failures of the detail pane's own actions (sensitivity, export, download,
  // speaker suggestions) — each one used to fail silently.
  const [detailError, setDetailError] = useState<string | null>(null);
  useEffect(() => setDetailError(null), [recordingId]);
  const config = useConfig();
  const perms = useMeetingPermissions(workspaceId);
  const can = (cap: string) => perms.data?.has(cap) ?? false;
  const suggestions = useSpeakerSuggestions(
    workspaceId,
    recordingId,
    detail.data?.status === "completed",
  );
  const acceptSuggestion = useAcceptSpeakerSuggestion(workspaceId, recordingId);
  const rejectSuggestion = useRejectSpeakerSuggestion(workspaceId, recordingId);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  // "loading" | "ready" | "error" | "unavailable" — the player region is always
  // visible with an explicit state, never silently absent.
  const [audioState, setAudioState] = useState<
    "loading" | "ready" | "error" | "unavailable"
  >("loading");
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playbackAttempt, setPlaybackAttempt] = useState(0);
  // Current playback position in ms — drives the active-segment highlight.
  const [currentMs, setCurrentMs] = useState(0);
  const [searchParams] = useSearchParams();
  const deepLinkAppliedRef = useRef(false);
  // Transcript, clips or sensitive-content review; ?tab= deep-links.
  const [view, setView] = useState<"transcript" | "notes" | "clips" | "dlp">(
    () => {
      const tab = searchParams.get("tab");
      return tab === "notes" || tab === "clips" || tab === "dlp"
        ? tab
        : "transcript";
    },
  );
  const [playbackRate, setPlaybackRate] = useState(1);
  const [revisionsOpen, setRevisionsOpen] = useState(false);
  const [commentsOpen, setCommentsOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [selectedSpeaker, setSelectedSpeaker] = useState<string | null>(null);
  const [followPlayback, setFollowPlayback] = useState(true);
  const queryClient = useQueryClient();
  const setSensitivity = useMutation({
    mutationFn: (level: MeetingRecording["sensitivity"]) =>
      setRecordingSensitivity(workspaceId, recordingId, level),
    onSuccess: (_data, level) =>
      toast.success(
        t("toast.sensitivityChanged", {
          level: t(SENSITIVITY_LABEL[level].labelKey),
        }),
      ),
    onError: (error) =>
      setDetailError(getErrorMessage(error, t("errors.sensitivity"))),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: meetingKeys.detail(workspaceId, recordingId),
      }),
  });
  // Client-side transcript search (issue #6, C4): matches keep their seek
  // buttons, so a hit jumps straight to the moment in the audio.
  const [query, setQuery] = useState("");
  const visibleSegments = useMemo(() => {
    const segments = detail.data?.segments ?? [];
    const needle = query.trim().toLowerCase();
    return segments.filter(
      (seg) =>
        (!selectedSpeaker || seg.speaker_label === selectedSpeaker) &&
        (!needle ||
          seg.text.toLowerCase().includes(needle) ||
          (
            seg.speaker_name ??
            speakerLabel(seg.speaker_label, (number) =>
              t("review.speaker", { number }),
            )
          )
            .toLowerCase()
            .includes(needle)),
    );
  }, [detail.data?.segments, query, selectedSpeaker, t]);

  const onExport = async (fmt: (typeof EXPORT_FORMATS)[number]) => {
    const revisionId = rec?.approved_revision_id ?? rec?.latest_revision_id;
    if (!rec || !revisionId) return;
    setDetailError(null);
    try {
      const { blob, filename } = await exportRevision(
        workspaceId,
        recordingId,
        revisionId,
        fmt,
      );
      downloadBlob(blob, filename ?? `${rec.title}.${fmt}`);
    } catch (error) {
      setDetailError(getErrorMessage(error, t("errors.export")));
    }
  };
  const revisions = useRevisions(
    workspaceId,
    recordingId,
    detail.data?.status === "completed",
  );
  // Discarded revisions are history, not something to review — the modal filters
  // them the same way, so the control and its contents agree (issue #39).
  const surviving = useMemo(
    () => (revisions.data ?? []).filter((r) => r.state !== "discarded"),
    [revisions.data],
  );

  const rec = detail.data;
  const isProcessing = rec ? !TERMINAL.includes(rec.status) : true;
  // Playback is available for ANY terminal recording whose audio still exists —
  // including failed ones (hearing the audio helps diagnose a failure).
  const canPlay =
    rec !== undefined && TERMINAL.includes(rec.status) && !rec.audio_deleted;
  const audioGone = rec !== undefined && rec.audio_deleted;

  useEffect(() => {
    // TRUE streaming (issue #6): the <audio> element gets a short-lived
    // token URL and streams/seeks with HTTP Range requests — the pre-#6 path
    // downloaded the entire recording as a Blob before playing.
    setAudioUrl(null);
    setCurrentMs(0);
    deepLinkAppliedRef.current = false;
    setAudioState(audioGone ? "unavailable" : "loading");
    let cancelled = false;
    if (canPlay && perms.data?.has("listen")) {
      createPlaybackSession(workspaceId, recordingId)
        .then(({ url }) => {
          if (cancelled) return;
          setAudioUrl(url);
          setAudioState("ready");
        })
        .catch(() => {
          if (cancelled) return;
          setAudioUrl(null);
          setAudioState("error");
        });
    }
    return () => {
      cancelled = true;
    };
  }, [
    workspaceId,
    recordingId,
    canPlay,
    audioGone,
    perms.data,
    playbackAttempt,
  ]);

  const seekTo = (ms: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = ms / 1000;
    setCurrentMs(ms);
    // play() may return undefined (older engines, jsdom) — guard the Promise.
    const playback = audio.play() as Promise<void> | undefined;
    void playback?.catch(() => {});
  };

  // Deep link (?t=seconds): open the recording at an exact moment.
  useEffect(() => {
    if (audioState !== "ready" || deepLinkAppliedRef.current) return;
    const t = Number(searchParams.get("t"));
    if (Number.isFinite(t) && t > 0) seekTo(t * 1000);
    deepLinkAppliedRef.current = true;
  }, [audioState, searchParams]);

  const skipBy = (deltaSeconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = Math.max(0, audio.currentTime + deltaSeconds);
  };

  const downloadAudio = async () => {
    setDetailError(null);
    try {
      const { blob, filename } = await downloadRecordingAudio(
        workspaceId,
        recordingId,
      );
      downloadBlob(blob, filename ?? `${rec?.title ?? "meeting"}.webm`);
    } catch (error) {
      setDetailError(getErrorMessage(error, t("errors.download")));
    }
  };

  if (detail.isPending || detail.isError) {
    return (
      <DataState
        isLoading={detail.isPending}
        error={detail.error}
        onRetry={() => void detail.refetch()}
        loadingLabel={t("review.loadingTranscript")}
      />
    );
  }

  return (
    <div className="meetings-detail">
      <CardHeader className="recording-detail-header flex flex-row items-start justify-between gap-3 p-5">
        <div className="min-w-0">
          <CardTitle>
            <h2 className="truncate" title={rec?.title}>
              {rec?.title}
            </h2>
          </CardTitle>
          <CardDescription className="mt-2 flex flex-wrap items-center gap-2">
            {rec && (
              <StatusLabel
                presentation={MEETING_STATUS[rec.status]}
                value={rec.status}
                isCompact
              />
            )}
            {rec?.duration_seconds != null && (
              <span>{fmtSeconds(rec.duration_seconds)}</span>
            )}
            {rec && (
              <time dateTime={rec.created_at}>
                {new Intl.DateTimeFormat(undefined, {
                  dateStyle: "medium",
                  timeStyle: "short",
                }).format(new Date(rec.created_at))}
              </time>
            )}
            {rec?.legal_hold && (
              <StatusLabel presentation={LEGAL_HOLD} isCompact />
            )}
          </CardDescription>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {rec?.status === "completed" && can("export") && (
            <DropdownMenu
              modal={false}
              open={exportOpen}
              onOpenChange={setExportOpen}
            >
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm">
                  <DownloadIcon data-icon="inline-start" />
                  {t("detail.export")}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuGroup>
                  {EXPORT_FORMATS.map((fmt) => (
                    <DropdownMenuItem
                      key={fmt}
                      onSelect={() => void onExport(fmt)}
                    >
                      {t(`detail.exportFormats.${fmt}`)}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          {rec && (
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t("review.moreActions")}
                >
                  <MoreHorizontalIcon data-icon="inline-start" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>
                    {t("review.moreActions")}
                  </DropdownMenuLabel>
                  {rec.status === "completed" &&
                    can("view_transcript") &&
                    (surviving.length > 1 ||
                      (surviving.length === 1 &&
                        can("approve_transcript"))) && (
                      <DropdownMenuItem onSelect={() => setRevisionsOpen(true)}>
                        {surviving.length > 1
                          ? t("detail.reviewRevisions")
                          : t("detail.reviewTranscript")}
                      </DropdownMenuItem>
                    )}
                  {rec.status === "completed" && can("view_transcript") && (
                    <DropdownMenuItem onSelect={() => setCommentsOpen(true)}>
                      {t("comments.open")}
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuSeparator />
                  {can("approve_transcript") ? (
                    <DropdownMenuSub>
                      <DropdownMenuSubTrigger>
                        {t("detail.sensitivityAria", {
                          level: t(SENSITIVITY_LABEL[rec.sensitivity].labelKey),
                        })}
                      </DropdownMenuSubTrigger>
                      <DropdownMenuSubContent>
                        <DropdownMenuGroup>
                          {SENSITIVITIES.map((level) => (
                            <DropdownMenuItem
                              key={level}
                              disabled={setSensitivity.isPending}
                              onSelect={() => {
                                setDetailError(null);
                                setSensitivity.mutate(level);
                              }}
                            >
                              {t(`detail.sensitivity.${level}`)}
                            </DropdownMenuItem>
                          ))}
                        </DropdownMenuGroup>
                      </DropdownMenuSubContent>
                    </DropdownMenuSub>
                  ) : (
                    <DropdownMenuLabel>
                      {t("detail.sensitivityAria", {
                        level: t(SENSITIVITY_LABEL[rec.sensitivity].labelKey),
                      })}
                    </DropdownMenuLabel>
                  )}
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </CardHeader>
      <Separator />
      <CardContent className="recording-detail-content px-5 pt-4 pb-0">
        {detailError && (
          <Alert
            variant="danger"
            isInline={true}
            isLiveRegion={true}
            title={detailError}
            actionClose={
              <Button
                aria-label={t("common:actions.close")}
                variant="ghost"
                size="icon-xs"
                onClick={() => setDetailError(null)}
              >
                <XIcon data-icon="inline-start" />
              </Button>
            }
          />
        )}
        {config.data?.meetings_status === "degraded" && (
          <Alert
            variant="warning"
            isInline={true}
            title={t("detail.serviceDegraded")}
          >
            {t("detail.serviceDegradedBody")}
          </Alert>
        )}
        {can("relabel_speakers") &&
          (suggestions.data?.length ?? 0) > 0 &&
          suggestions.data!.map((sug) => (
            <Alert
              key={sug.id}
              variant="info"
              isInline={true}
              title={t("detail.suggestionTitle", {
                name: sug.suggested_profile_name ?? "?",
                label: speakerLabel(sug.speaker_label, (number) =>
                  t("review.speaker", { number }),
                ),
                pct: Math.round(sug.score * 100),
              })}
            >
              <div className="flex flex-wrap items-center gap-2">
                <div>
                  <Button
                    onClick={() =>
                      acceptSuggestion.mutate(sug.id, {
                        onError: (error) =>
                          setDetailError(
                            getErrorMessage(error, t("errors.suggestion")),
                          ),
                      })
                    }
                    disabled={acceptSuggestion.isPending}
                    variant="outline"
                    size="sm"
                  >
                    {t("detail.suggestionConfirm")}
                  </Button>
                </div>
                <div>
                  <Button
                    onClick={() =>
                      rejectSuggestion.mutate(sug.id, {
                        onError: (error) =>
                          setDetailError(
                            getErrorMessage(error, t("errors.suggestion")),
                          ),
                      })
                    }
                    disabled={rejectSuggestion.isPending}
                    variant="link"
                    size="sm"
                  >
                    {t("detail.suggestionDismiss")}
                  </Button>
                </div>
              </div>
            </Alert>
          ))}
        {isProcessing && (
          <div className="flex flex-wrap items-center gap-2">
            {rec && (
              <div>
                <StatusLabel
                  presentation={MEETING_STATUS[rec.status]}
                  value={rec.status}
                />
              </div>
            )}
            <div>
              <p role="status" className="app-copy">
                {t(`processing.${rec?.current_step ?? "queued"}`, {
                  defaultValue: t("detail.processing"),
                })}
              </p>
            </div>
          </div>
        )}
        {rec?.status === "failed" && (
          <Alert variant="danger" isInline={true} title={t("detail.failed")}>
            {meetingErrorMessage(rec.error_code, t)}
          </Alert>
        )}
        {/* Player region — always visible for a finished recording, with an
          explicit state instead of silently missing audio. */}
        {rec !== undefined && !isProcessing && can("listen") && (
          <section
            aria-label={t("detail.playback")}
            className="meeting-playback"
          >
            <div className="w-full min-w-0">
              {audioState === "ready" && audioUrl ? (
                <div className="meeting-audio-shell">
                  {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
                  <audio
                    controls={true}
                    preload="metadata"
                    aria-label={t("detail.playback")}
                    ref={audioRef}
                    src={audioUrl}
                    className="meeting-audio-player"
                    onLoadedMetadata={(e) => {
                      e.currentTarget.playbackRate = playbackRate;
                    }}
                    onError={() => setAudioState("error")}
                    onTimeUpdate={(e) =>
                      setCurrentMs(e.currentTarget.currentTime * 1000)
                    }
                  />
                </div>
              ) : audioState === "loading" ? (
                <small className="app-copy">{t("detail.audioLoading")}</small>
              ) : audioState === "unavailable" ? (
                <small className="app-copy">
                  {t("detail.audioUnavailable")}
                </small>
              ) : (
                <Alert
                  variant="warning"
                  isInline={true}
                  title={t("detail.audioError")}
                >
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setPlaybackAttempt((attempt) => attempt + 1)}
                  >
                    {t("common:dataState.retry")}
                  </Button>
                </Alert>
              )}
            </div>
            <div className="meeting-playback-tools">
              {audioState === "ready" && (
                <>
                  <div>
                    <Button
                      aria-label={t("detail.skipBack")}
                      onClick={() => skipBy(-10)}
                      variant="ghost"
                      size="sm"
                    >
                      <RotateCcwIcon data-icon="inline-start" />
                      {t("detail.skipBackShort")}
                    </Button>
                    <Button
                      aria-label={t("detail.skipForward")}
                      onClick={() => skipBy(10)}
                      variant="ghost"
                      size="sm"
                    >
                      <RotateCwIcon data-icon="inline-start" />
                      {t("detail.skipForwardShort")}
                    </Button>
                  </div>
                  <div>
                    <NativeSelect
                      value={String(playbackRate)}
                      aria-label={t("detail.playbackSpeed")}
                      className="meeting-speed-select"
                      onChange={(_e) => {
                        const v = _e.currentTarget.value;
                        const rate = Number(v);
                        setPlaybackRate(rate);
                        if (audioRef.current)
                          audioRef.current.playbackRate = rate;
                      }}
                    >
                      {[0.75, 1, 1.25, 1.5, 2].map((rate) => (
                        <NativeSelectOption key={rate} value={String(rate)}>
                          {t("detail.speedOption", { rate })}
                        </NativeSelectOption>
                      ))}
                    </NativeSelect>
                  </div>
                </>
              )}
              {audioState !== "unavailable" && can("download") && (
                <div className="ml-auto">
                  <Button
                    onClick={downloadAudio}
                    variant="ghost"
                    size="icon-sm"
                    aria-label={t("detail.downloadAudio")}
                    title={t("detail.downloadAudio")}
                  >
                    <DownloadIcon data-icon="inline-start" />
                  </Button>
                </div>
              )}
            </div>
          </section>
        )}
        {commentsOpen && rec && (
          <MeetingCommentsDrawer
            workspaceId={workspaceId}
            recordingId={recordingId}
            recordingTitle={rec.title}
            canComment={canEdit && can("view_transcript")}
            canModerate={isOwner}
            anchorMs={currentMs}
            onSeek={seekTo}
            onClose={() => setCommentsOpen(false)}
          />
        )}
        {revisionsOpen && rec && (
          <MeetingRevisionsModal
            workspaceId={workspaceId}
            recordingId={recordingId}
            displayRevisionId={
              rec.approved_revision_id ?? rec.latest_revision_id
            }
            canEdit={can("approve_transcript")}
            onClose={() => setRevisionsOpen(false)}
          />
        )}
        {rec?.status === "completed" && (
          <Tabs
            value={view}
            onValueChange={(value) => setView(value as typeof view)}
            className="transcript-tabs min-h-0 flex-1 gap-0"
          >
            <TabsList
              variant="line"
              aria-label={t("review.views")}
              // Four tabs are wider than a phone: the row scrolls, not the page.
              className="max-w-full justify-start overflow-x-auto overflow-y-hidden"
            >
              <TabsTrigger value="transcript">
                {t("review.transcript")}
              </TabsTrigger>
              <TabsTrigger value="notes">{t("notes:tab")}</TabsTrigger>
              <TabsTrigger value="clips">{t("clips.viewClips")}</TabsTrigger>
              {can("redact") && (
                <TabsTrigger value="dlp">{t("dlp.viewDlp")}</TabsTrigger>
              )}
            </TabsList>
            <Separator />
            <TabsContent value="transcript" className="transcript-panel">
              {view === "transcript" &&
                rec?.status === "completed" &&
                rec.segments.length > 0 && (
                  <>
                    <div className="transcript-toolbar">
                      <SearchInput
                        placeholder={t("detail.searchPlaceholder")}
                        aria-label={t("detail.searchAria")}
                        value={query}
                        onChange={(_e, v) => setQuery(v)}
                        onClear={() => setQuery("")}
                        resultsCount={
                          query || selectedSpeaker
                            ? visibleSegments.length
                            : undefined
                        }
                      />
                      <Field
                        orientation="horizontal"
                        className="w-auto shrink-0"
                      >
                        <Checkbox
                          id="follow-playback"
                          checked={followPlayback}
                          onCheckedChange={(state) => {
                            const checked = state === true;
                            return setFollowPlayback(checked);
                          }}
                        />
                        <FieldLabel htmlFor="follow-playback">
                          {t("review.followPlayback")}
                        </FieldLabel>
                      </Field>
                    </div>
                    <SpeakerFilter
                      segments={rec.segments}
                      selected={selectedSpeaker}
                      onSelect={setSelectedSpeaker}
                    />
                    <div className="transcript-timeline">
                      <div className="flex items-center justify-between text-xs text-muted-foreground">
                        <span>{t("review.speakerActivity")}</span>
                        <span>
                          {fmtTs(currentMs)} /{" "}
                          {fmtSeconds(
                            Math.max(
                              rec.duration_seconds ?? 0,
                              ...rec.segments.map((s) => s.end_ms / 1000),
                            ),
                          )}
                        </span>
                      </div>
                      <MeetingTimeline
                        segments={rec.segments}
                        currentMs={currentMs}
                        onSeek={seekTo}
                      />
                    </div>
                  </>
                )}
              {view === "transcript" && rec?.status === "completed" && (
                <div className="transcript-scroll">
                  <p className="transcript-help">
                    {t(
                      can("relabel_speakers")
                        ? "review.help"
                        : "review.listenHelp",
                    )}
                  </p>
                  <DataState
                    isLoading={false}
                    isEmpty={visibleSegments.length === 0}
                    isFiltered={Boolean(query || selectedSpeaker)}
                    onClearFilters={() => {
                      setQuery("");
                      setSelectedSpeaker(null);
                    }}
                    emptyTitle={
                      can("view_transcript")
                        ? t("review.noSpeech")
                        : t("review.noAccess")
                    }
                    emptyBody={
                      can("view_transcript")
                        ? t("review.noSpeechBody")
                        : t("review.noAccessBody")
                    }
                  >
                    <div className="transcript-document">
                      {visibleSegments.map((seg) => (
                        <SegmentRow
                          key={seg.id}
                          workspaceId={workspaceId}
                          recordingId={recordingId}
                          segment={seg}
                          canEdit={can("edit_transcript")}
                          canRelabel={can("relabel_speakers")}
                          canRedact={can("redact")}
                          followPlayback={followPlayback}
                          isActive={
                            currentMs >= seg.start_ms && currentMs < seg.end_ms
                          }
                          onSeek={seekTo}
                        />
                      ))}
                    </div>
                  </DataState>
                </div>
              )}
            </TabsContent>
            <TabsContent value="notes" className="transcript-scroll">
              {view === "notes" && (
                <MeetingNotesPanel
                  workspaceId={workspaceId}
                  recordingId={recordingId}
                  displayRevisionId={
                    rec.approved_revision_id ?? rec.latest_revision_id ?? null
                  }
                  canCreate={can("create_notes")}
                  canExport={can("export")}
                  isAvailable={config.data?.notes_status === "available"}
                />
              )}
            </TabsContent>
            <TabsContent value="clips" className="transcript-scroll">
              <MeetingClipsPanel
                workspaceId={workspaceId}
                recordingId={recordingId}
                canClip={can("create_clip")}
                currentMs={currentMs}
              />
            </TabsContent>
            {can("redact") && (
              <TabsContent value="dlp" className="transcript-scroll">
                <MeetingDlpPanel
                  workspaceId={workspaceId}
                  recordingId={recordingId}
                  onSeek={seekTo}
                />
              </TabsContent>
            )}
          </Tabs>
        )}
      </CardContent>
    </div>
  );
}

function SegmentRow({
  workspaceId,
  recordingId,
  segment,
  canEdit,
  canRelabel,
  canRedact,
  isActive,
  followPlayback,
  onSeek,
}: {
  workspaceId: string;
  recordingId: string;
  segment: MeetingSegment;
  canEdit: boolean;
  canRelabel: boolean;
  /** May mark this segment for redaction (issue #6, E4). */
  canRedact: boolean;
  /** Playback is currently inside this segment (highlight + auto-scroll). */
  isActive: boolean;
  followPlayback: boolean;
  /** Seek playback to a position in ms (click-to-seek, issue #6). */
  onSeek: (ms: number) => void;
}) {
  const { t } = useTranslation(["meetings", "common"]);
  const speakers = useSpeakers(canRelabel);
  const update = useUpdateSegment(workspaceId, recordingId);
  const assignAll = useAssignSpeakerByLabel(workspaceId, recordingId);
  const markRedaction = useAddManualDlpFinding(workspaceId, recordingId);
  const [open, setOpen] = useState(false);
  // Inline text editing (issue #6, C1): optimistic via expected_version — a
  // concurrent change 409s and the transcript refreshes instead of silently
  // overwriting someone else's correction.
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [conflict, setConflict] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);
  const onRowError = (error: unknown) =>
    setRowError(getErrorMessage(error, t("errors.segment")));
  const rowRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Follow playback through the transcript without yanking the viewport.
    if (isActive && followPlayback && !editing)
      rowRef.current?.scrollIntoView({ block: "nearest" });
  }, [isActive, followPlayback, editing]);

  const beginEdit = () => {
    setDraft(segment.text);
    setConflict(false);
    setEditing(true);
  };

  const saveEdit = () => {
    update.mutate(
      {
        segmentId: segment.id,
        text: draft,
        expected_version: segment.version,
      },
      {
        onSuccess: () => setEditing(false),
        onError: (e) => {
          const status = (e as { response?: { status?: number } })?.response
            ?.status;
          if (status === 409) setConflict(true);
          else onRowError(e);
        },
      },
    );
  };

  const friendlyLabel = speakerLabel(segment.speaker_label, (number) =>
    t("review.speaker", { number }),
  );
  const name = segment.speaker_name ?? friendlyLabel;

  return (
    <article
      ref={rowRef}
      className={cn("meeting-seg", isActive && "meeting-seg-active")}
    >
      <div className="flex flex-wrap items-center gap-1">
        <div>
          <Button
            onClick={() => onSeek(segment.start_ms)}
            aria-label={t("detail.seekTo", { ts: fmtTs(segment.start_ms) })}
            variant="link"
            size="sm"
          >
            {fmtTs(segment.start_ms)}
          </Button>
        </div>
        <div>
          {canRelabel ? (
            <DropdownMenu modal={false} open={open} onOpenChange={setOpen}>
              <DropdownMenuTrigger asChild={true}>
                <Button
                  aria-label={t("review.assignNamedSpeaker", { name })}
                  variant="ghost"
                  size="sm"
                >
                  <strong>{name}</strong>
                  <ChevronDownIcon data-icon="inline-end" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuGroup>
                  <>
                    <DropdownMenuGroup>
                      <DropdownMenuLabel>
                        {t("detail.thisTurn")}
                      </DropdownMenuLabel>
                      <DropdownMenuItem
                        key="clear"
                        onClick={() =>
                          update.mutate(
                            { segmentId: segment.id, clear_speaker: true },
                            { onError: onRowError },
                          )
                        }
                      >
                        {t("detail.anonymous")}
                      </DropdownMenuItem>
                      {(speakers.data ?? []).map((sp) => (
                        <DropdownMenuItem
                          key={sp.id}
                          onClick={() =>
                            update.mutate(
                              {
                                segmentId: segment.id,
                                speaker_profile_id: sp.id,
                              },
                              { onError: onRowError },
                            )
                          }
                        >
                          {sp.display_name}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuGroup>
                    {/* Bulk correction: "I recognize this voice" — applies the
                    person to EVERY turn diarized as this label. Annotation
                    only; enrolling a voiceprint stays a consent-gated,
                    self-service action (ARCHITECTURE.md). */}
                    <DropdownMenuGroup>
                      <DropdownMenuLabel>
                        {t("detail.allTurnsFor", { label: friendlyLabel })}
                      </DropdownMenuLabel>
                      <DropdownMenuItem
                        key="clear-all"
                        onClick={() =>
                          assignAll.mutate(
                            {
                              speaker_label: segment.speaker_label,
                              clear_speaker: true,
                            },
                            { onError: onRowError },
                          )
                        }
                      >
                        {t("detail.anonymous")}
                      </DropdownMenuItem>
                      {(speakers.data ?? []).map((sp) => (
                        <DropdownMenuItem
                          key={`all-${sp.id}`}
                          onClick={() =>
                            assignAll.mutate(
                              {
                                speaker_label: segment.speaker_label,
                                speaker_profile_id: sp.id,
                              },
                              { onError: onRowError },
                            )
                          }
                        >
                          {sp.display_name}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuGroup>
                  </>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : (
            <p className="app-copy">
              <strong>{name}</strong>
            </p>
          )}
        </div>
        {segment.is_edited && (
          <div>
            <StatusLabel presentation={EDITED_BADGE} isCompact={true} />
          </div>
        )}
        {canEdit && !editing && (
          <div className="ml-auto">
            <Button
              onClick={beginEdit}
              variant="ghost"
              size="sm"
              aria-label={t("detail.editText")}
              title={t("detail.editText")}
            >
              <PencilIcon data-icon="inline-start" />
              <span className="hidden sm:inline">{t("detail.editText")}</span>
            </Button>
          </div>
        )}
        {canRedact && !editing && (
          <RowActions
            toggleAriaLabel={t("review.segmentActions", {
              ts: fmtTs(segment.start_ms),
            })}
            items={[
              {
                title: t("dlp.markSegment"),
                isDanger: true,
                isDisabled: markRedaction.isPending,
                onClick: () =>
                  markRedaction.mutate(
                    {
                      segment_id: segment.id,
                      start_char: 0,
                      end_char: segment.text.length,
                    },
                    { onError: onRowError },
                  ),
              },
            ]}
          />
        )}
      </div>
      {rowError && (
        <Alert
          variant="danger"
          isInline={true}
          isLiveRegion={true}
          title={rowError}
          actionClose={
            <Button
              aria-label={t("common:actions.close")}
              variant="ghost"
              size="icon-xs"
              onClick={() => setRowError(null)}
            >
              <XIcon data-icon="inline-start" />
            </Button>
          }
        />
      )}
      {editing ? (
        <>
          <Textarea
            value={draft}
            aria-label={t("detail.editText")}
            onChange={(_e) => {
              const v = _e.currentTarget.value;
              return setDraft(v);
            }}
          />
          {conflict && (
            <Alert
              variant="warning"
              isInline={true}
              isLiveRegion={true}
              title={t("detail.editConflict")}
            />
          )}
          <div className="flex flex-wrap items-center gap-2">
            <div>
              <Button
                onClick={saveEdit}
                disabled={update.isPending || !draft.trim()}
                aria-busy={update.isPending}
                variant="default"
                size="sm"
              >
                {update.isPending && (
                  <Spinner data-icon="inline-start" aria-label={undefined} />
                )}
                {t("detail.save")}
              </Button>
            </div>
            <div>
              <Button
                onClick={() => setEditing(false)}
                variant="link"
                size="sm"
              >
                {t("common:actions.cancel")}
              </Button>
            </div>
          </div>
        </>
      ) : (
        <p className="transcript-text">{segment.text}</p>
      )}
    </article>
  );
}
