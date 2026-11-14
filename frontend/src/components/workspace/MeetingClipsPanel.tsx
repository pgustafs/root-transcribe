import { ScissorsIcon as CutIcon, XIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { FieldDescription } from "@/components/ui/field";

import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import { createClipPlaybackSession } from "@/api/meetings";
import type { MeetingClip } from "@/api/types";
import { Alert } from "@/components/Alert";
import { ConfirmDeleteModal } from "@/components/ConfirmDeleteModal";
import { DataState } from "@/components/DataState";
import { StatusLabel } from "@/components/StatusLabel";
import { useToast } from "@/components/toast/useToast";
import { useClips, useCreateClip, useDeleteClip } from "@/hooks/useMeetings";
import { danger, info, type StatusPresentation, success } from "@/lib/labels";
import { formatMsClock } from "@/utils/formatDuration";

const CLIP_STATUS: Record<MeetingClip["status"], StatusPresentation> = {
  pending: info("meetings:clips.status.pending", true),
  processing: info("meetings:clips.status.processing", true),
  ready: success("meetings:clips.status.ready"),
  failed: danger("meetings:clips.status.failed"),
};

export function MeetingClipsPanel({
  workspaceId,
  recordingId,
  canClip,
  currentMs,
}: {
  workspaceId: string;
  recordingId: string;
  /** The caller holds the create_clip capability (server-computed). */
  canClip: boolean;
  /** Current playback position — the clip-bound source of truth. */
  currentMs: number;
}) {
  const { t } = useTranslation(["meetings", "common"]);
  const clips = useClips(workspaceId, recordingId);
  const create = useCreateClip(workspaceId, recordingId);
  const remove = useDeleteClip(workspaceId, recordingId);
  const [title, setTitle] = useState("");
  const [startMs, setStartMs] = useState<number | null>(null);
  const [endMs, setEndMs] = useState<number | null>(null);
  // The clip currently playing inline (id -> token URL).
  const [playing, setPlaying] = useState<{ id: string; url: string } | null>(
    null,
  );
  const [deleting, setDeleting] = useState<MeetingClip | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const toast = useToast();

  const canSubmit =
    title.trim() !== "" &&
    startMs !== null &&
    endMs !== null &&
    endMs > startMs &&
    !create.isPending;

  const invalidRange = startMs !== null && endMs !== null && endMs <= startMs;

  const play = async (clip: MeetingClip) => {
    setActionError(null);
    try {
      const { url } = await createClipPlaybackSession(
        workspaceId,
        recordingId,
        clip.id,
      );
      setPlaying({ id: clip.id, url });
    } catch (error) {
      setActionError(getErrorMessage(error, t("clips.playFailed")));
    }
  };

  const confirmDelete = () => {
    if (!deleting) return;
    const { id, title: clipTitle } = deleting;
    remove.mutate(id, {
      onSuccess: () => {
        setDeleting(null);
        if (playing?.id === id) setPlaying(null);
        toast.success(t("clips.toastDeleted", { title: clipTitle }));
      },
      onError: (error) => {
        setDeleting(null);
        setActionError(getErrorMessage(error, t("clips.deleteFailed")));
      },
    });
  };

  const rows = clips.data ?? [];
  return (
    <div className="flex flex-col gap-4">
      {canClip && (
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <div>
              <Input
                id="meeting-clip-title"
                value={title}
                aria-label={t("clips.titleLabel")}
                placeholder={t("clips.titlePlaceholder")}
                maxLength={255}
                onChange={(_e) => {
                  const v = _e.currentTarget.value;
                  return setTitle(v);
                }}
              />
            </div>
            <div>
              <Button
                onClick={() => setStartMs(Math.floor(currentMs))}
                variant="outline"
                size="sm"
              >
                {startMs === null
                  ? t("clips.setStart")
                  : t("clips.startAt", { time: formatMsClock(startMs) })}
              </Button>
            </div>
            <div>
              <Button
                onClick={() => setEndMs(Math.floor(currentMs))}
                variant="outline"
                size="sm"
              >
                {endMs === null
                  ? t("clips.setEnd")
                  : t("clips.endAt", { time: formatMsClock(endMs) })}
              </Button>
            </div>
            <div>
              <Button
                onClick={() => {
                  if (startMs === null || endMs === null) return;
                  create.mutate(
                    { title: title.trim(), start_ms: startMs, end_ms: endMs },
                    {
                      onSuccess: (clip) => {
                        setTitle("");
                        setStartMs(null);
                        setEndMs(null);
                        toast.success(
                          t("clips.toastCreated", { title: clip.title }),
                        );
                      },
                    },
                  );
                }}
                disabled={!canSubmit || create.isPending}
                aria-busy={create.isPending}
                variant="default"
                size="sm"
              >
                {create.isPending && (
                  <Spinner
                    data-icon="inline-start"
                    aria-label={t("clips.creating")}
                  />
                )}
                {t("clips.create")}
              </Button>
            </div>
          </div>
          {invalidRange && (
            <div>
              <FieldDescription>{t("clips.invalidRange")}</FieldDescription>
            </div>
          )}
        </div>
      )}
      {create.isError && (
        <div>
          <Alert
            variant="danger"
            isInline={true}
            isLiveRegion={true}
            title={t("clips.createFailed")}
          >
            {getErrorMessage(create.error, "")}
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
          isLoading={clips.isLoading}
          error={clips.error}
          onRetry={() => void clips.refetch()}
          isEmpty={rows.length === 0}
          emptyTitle={t("clips.emptyTitle")}
          emptyBody={t("clips.emptyBody")}
          emptyIcon={CutIcon}
          headingLevel="h3"
        >
          <div className="flex flex-col gap-4">
            {rows.map((clip) => (
              <div key={clip.id}>
                <div className="flex flex-wrap items-center gap-2">
                  <div>
                    <p className="app-copy">
                      {clip.title}{" "}
                      <small className="app-copy">
                        {t("clips.range", {
                          start: formatMsClock(clip.start_ms),
                          end: formatMsClock(clip.end_ms),
                        })}
                      </small>
                    </p>
                  </div>
                  <div>
                    <StatusLabel
                      presentation={CLIP_STATUS[clip.status]}
                      value={clip.status}
                      isCompact={true}
                    />
                  </div>
                  {clip.status === "ready" && (
                    <div>
                      <Button
                        aria-label={t("clips.playAria", { title: clip.title })}
                        onClick={() => void play(clip)}
                        variant="link"
                        size="sm"
                      >
                        {t("clips.play")}
                      </Button>
                    </div>
                  )}
                  {canClip && (
                    <div>
                      <Button
                        aria-label={t("clips.deleteAria", {
                          title: clip.title,
                        })}
                        onClick={() => setDeleting(clip)}
                        variant="destructive"
                        size="sm"
                      >
                        {t("common:actions.delete")}
                      </Button>
                    </div>
                  )}
                </div>
                {clip.status === "failed" && clip.error && (
                  <small className="app-copy">{clip.error}</small>
                )}
                {playing?.id === clip.id && (
                  <div className="meeting-audio-shell">
                    {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
                    <audio
                      controls={true}
                      autoPlay={true}
                      src={playing.url}
                      className="meeting-audio-player"
                      aria-label={t("clips.playerAria", { title: clip.title })}
                    />
                  </div>
                )}
              </div>
            ))}
          </div>
        </DataState>
      </div>
      {deleting && (
        <ConfirmDeleteModal
          isOpen={true}
          title={t("clips.deleteTitle")}
          itemName={deleting.title}
          consequence={t("clips.deleteConsequence")}
          isDeleting={remove.isPending}
          onConfirm={confirmDelete}
          onClose={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
