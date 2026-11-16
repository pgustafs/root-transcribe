import { MoreVerticalIcon as EllipsisVIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenuItem,
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuGroup,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import {
  DialogHeader,
  DialogTitle,
  DialogFooter,
  Dialog,
  DialogContent,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { Field, FieldLabel } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { Spinner } from "@/components/ui/spinner";
import { useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";
import type { MeetingComment } from "@/api/types";
import { useAuth } from "@/auth/useAuth";
import { Alert } from "@/components/Alert";
import { DataState } from "@/components/DataState";
import { StatusLabel } from "@/components/StatusLabel";
import { useToast } from "@/components/toast/useToast";
import {
  useAddMeetingComment,
  useDeleteMeetingComment,
  useMeetingComments,
  useResolveMeetingComment,
  useUpdateMeetingComment,
} from "@/hooks/useMeetings";
import { useWorkspaceMembers } from "@/hooks/useWorkspaces";
import { formatMsClock } from "@/utils/formatDuration";
import { formatRelativeTime } from "@/utils/relativeTime";
import { success } from "@/lib/labels";

const RESOLVED = success("access:workspaces.comments.resolved");

export function MeetingCommentsDrawer({
  workspaceId,
  recordingId,
  recordingTitle,
  canComment,
  canModerate,
  anchorMs,
  onSeek,
  onClose,
}: {
  workspaceId: string;
  recordingId: string;
  recordingTitle: string;
  /** Editor+: may post, reply, resolve. Viewers read only. */
  canComment: boolean;
  /** Workspace owner: may delete anyone's comment. */
  canModerate: boolean;
  /** Playback position when the dialog opened — offered as the anchor. */
  anchorMs: number;
  /** Seek playback to a position in ms (anchor chips). */
  onSeek: (ms: number) => void;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation(["meetings", "access", "common"]);
  const { user } = useAuth();
  const [showResolved, setShowResolved] = useState(false);
  const commentsQuery = useMeetingComments(
    workspaceId,
    recordingId,
    showResolved,
  );
  const { data: members = [] } = useWorkspaceMembers(workspaceId);
  const addComment = useAddMeetingComment(workspaceId, recordingId);
  const updateComment = useUpdateMeetingComment(workspaceId, recordingId);
  const deleteComment = useDeleteMeetingComment(workspaceId, recordingId);
  const resolveComment = useResolveMeetingComment(workspaceId, recordingId);

  const [body, setBody] = useState("");
  const [anchor, setAnchor] = useState<number | null>(anchorMs);
  const [replyTo, setReplyTo] = useState<MeetingComment | null>(null);
  const [editing, setEditing] = useState<MeetingComment | null>(null);
  const [editBody, setEditBody] = useState("");
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  // The @mention typeahead: the partial name being typed (null = inactive).
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [mentionIds, setMentionIds] = useState<string[]>([]);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const toast = useToast();
  // Every comment write reports its failure here — they used to fail silently
  // (and the composer's mutateAsync rejected unhandled).
  const [actionError, setActionError] = useState<string | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const onActionError = (error: unknown) =>
    setActionError(getErrorMessage(error, t("comments.actionFailed")));

  const comments = useMemo(
    () => commentsQuery.data ?? [],
    [commentsQuery.data],
  );
  const roots = comments.filter((c) => c.parent_id === null);
  const repliesOf = (rootId: string) =>
    comments.filter((c) => c.parent_id === rootId);

  const mentionCandidates = useMemo(() => {
    if (mentionQuery === null) return [];
    const q = mentionQuery.toLowerCase();
    return members
      .filter((m) => m.email && m.email.toLowerCase().includes(q))
      .slice(0, 5);
  }, [members, mentionQuery]);

  const onBodyChange = (value: string) => {
    setBody(value);
    // Typeahead activates on "@" followed by a partial address.
    const match = /@([\w.\-+]*)$/.exec(value);
    setMentionQuery(match ? match[1] : null);
  };

  const pickMention = (userId: string, email: string) => {
    setBody((prev) => prev.replace(/@[\w.\-+]*$/, `@${email} `));
    setMentionIds((prev) => (prev.includes(userId) ? prev : [...prev, userId]));
    setMentionQuery(null);
    bodyRef.current?.focus();
  };

  const submit = async () => {
    const trimmed = body.trim();
    if (!trimmed) return;
    setActionError(null);
    try {
      await addComment.mutateAsync({
        body: trimmed,
        parent_id: replyTo?.id,
        at_ms: replyTo ? undefined : (anchor ?? undefined),
        mentions: mentionIds.length ? mentionIds : undefined,
      });
    } catch (error) {
      onActionError(error);
      return;
    }
    setBody("");
    setReplyTo(null);
    setMentionIds([]);
  };

  const commentBlock = (comment: MeetingComment, isReply: boolean) => (
    <div
      key={comment.id}
      className={cn("ws-comment", isReply && "ws-comment--reply")}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <small className="app-copy">
            <strong>
              {comment.author_name ??
                comment.author_email ??
                t("access:workspaces.comments.deletedUser")}
            </strong>
            {" · "}
            {formatRelativeTime(comment.created_at, i18n.language)}
            {comment.at_ms !== null && (
              <>
                {" "}
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => onSeek(comment.at_ms!)}
                >
                  {formatMsClock(comment.at_ms)}
                </Button>
              </>
            )}
            {comment.resolved_at && (
              <>
                {" "}
                <StatusLabel presentation={RESOLVED} isCompact={true} />
              </>
            )}
          </small>
        </div>
        <div>
          <DropdownMenu
            open={openMenuId === comment.id}
            onOpenChange={(open) => setOpenMenuId(open ? comment.id : null)}
          >
            <DropdownMenuTrigger
              render={
                <Button
                  aria-label={t("comments.actionsFor", {
                    name:
                      comment.author_name ??
                      comment.author_email ??
                      t("access:workspaces.comments.deletedUser"),
                  })}
                  variant="ghost"
                  size="icon"
                />
              }
            >
              {<EllipsisVIcon data-icon="inline-start" />}
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                <>
                  {!isReply && canComment && (
                    <DropdownMenuItem
                      onClick={() =>
                        resolveComment.mutate(
                          {
                            commentId: comment.id,
                            resolved: !comment.resolved_at,
                          },
                          { onError: onActionError },
                        )
                      }
                    >
                      {comment.resolved_at
                        ? t("access:workspaces.comments.reopen")
                        : t("access:workspaces.comments.resolve")}
                    </DropdownMenuItem>
                  )}
                  {comment.author_id === user?.id && (
                    <DropdownMenuItem
                      onClick={() => {
                        setEditError(null);
                        setEditing(comment);
                        setEditBody(comment.body);
                      }}
                    >
                      {t("common:actions.edit")}
                    </DropdownMenuItem>
                  )}
                  {(comment.author_id === user?.id || canModerate) && (
                    <DropdownMenuItem
                      onClick={() =>
                        deleteComment.mutate(comment.id, {
                          onSuccess: () =>
                            toast.success(t("comments.toastDeleted")),
                          onError: onActionError,
                        })
                      }
                      variant="destructive"
                    >
                      {t("common:actions.delete")}
                    </DropdownMenuItem>
                  )}
                </>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      {comment.anchor_quote && (
        <blockquote className={cn("app-copy", "ws-comment-quote")}>
          {comment.anchor_quote}
        </blockquote>
      )}
      <p className={cn("app-copy", "ws-comment-body")}>{comment.body}</p>
      {!isReply && canComment && (
        <Button
          onClick={() => {
            setReplyTo(comment);
            bodyRef.current?.focus();
          }}
          variant="link"
        >
          {t("access:workspaces.comments.reply")}
        </Button>
      )}
    </div>
  );

  return (
    <Dialog
      open={true}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        aria-labelledby="meeting-comments-title"
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl"
      >
        <DialogHeader>
          <DialogTitle id="meeting-comments-title">
            {t("comments.title", { title: recordingTitle })}
          </DialogTitle>
        </DialogHeader>
        <div className="min-w-0">
          <div className="flex flex-col gap-4">
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
              <Field orientation="horizontal">
                <Switch
                  id="meeting-comments-show-resolved"
                  checked={showResolved}
                  onCheckedChange={(state) => {
                    const checked = state === true;
                    return setShowResolved(checked);
                  }}
                />
                <FieldLabel htmlFor="meeting-comments-show-resolved">
                  {t("access:workspaces.comments.showResolved")}
                </FieldLabel>
              </Field>
            </div>
            <div>
              <DataState
                isLoading={commentsQuery.isLoading}
                error={commentsQuery.error}
                isEmpty={roots.length === 0}
                emptyTitle={t("access:workspaces.comments.empty")}
                emptyBody={t("comments.emptyBody")}
              >
                <div className="flex flex-col gap-4">
                  {roots.map((root) => (
                    <div key={root.id}>
                      <div className="flex flex-col">
                        {commentBlock(root, false)}
                        {repliesOf(root.id).map((reply) =>
                          commentBlock(reply, true),
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </DataState>
            </div>
            {canComment && (
              <div>
                <div className="flex flex-col">
                  {replyTo && (
                    <div>
                      <small className="app-copy">
                        {t("access:workspaces.comments.replyingTo", {
                          name:
                            replyTo.author_name ??
                            replyTo.author_email ??
                            t("access:workspaces.comments.deletedUser"),
                        })}{" "}
                        <Button onClick={() => setReplyTo(null)} variant="link">
                          {t("common:actions.cancel")}
                        </Button>
                      </small>
                    </div>
                  )}
                  {!replyTo && anchor !== null && (
                    <div>
                      <small className="app-copy">
                        {t("comments.anchoredAt", {
                          time: formatMsClock(anchor),
                        })}{" "}
                        <Button onClick={() => setAnchor(null)} variant="link">
                          {t("comments.removeAnchor")}
                        </Button>
                      </small>
                    </div>
                  )}
                  <div className="ws-comment-composer">
                    <Textarea
                      ref={bodyRef}
                      value={body}
                      onKeyDown={(e) => {
                        // Keyboard path into the @mention typeahead: Escape
                        // dismisses it (without closing the modal), ArrowDown
                        // moves focus to its first item.
                        if (mentionQuery === null) return;
                        if (e.key === "Escape") {
                          e.stopPropagation();
                          setMentionQuery(null);
                        } else if (
                          e.key === "ArrowDown" &&
                          mentionCandidates.length > 0
                        ) {
                          e.preventDefault();
                          bodyRef.current
                            ?.closest(".ws-comment-composer")
                            ?.querySelector<HTMLButtonElement>(
                              "button:not(:disabled)",
                            )
                            ?.focus();
                        }
                      }}
                      rows={3}
                      aria-label={t("access:workspaces.comments.bodyAria")}
                      placeholder={t(
                        "access:workspaces.comments.bodyPlaceholder",
                      )}
                      onChange={(_e) => {
                        const value = _e.currentTarget.value;
                        return onBodyChange(value);
                      }}
                    />
                    {mentionQuery !== null && mentionCandidates.length > 0 && (
                      <div
                        className={cn(
                          "rounded-md border bg-popover p-1 shadow-md",
                          "ws-comment-mentions",
                        )}
                      >
                        <div>
                          {mentionCandidates.map((member) => (
                            <Button
                              key={member.user_id}
                              onClick={() =>
                                pickMention(
                                  member.user_id,
                                  member.email ?? member.user_id,
                                )
                              }
                              variant="ghost"
                              className="w-full justify-start"
                            >
                              {member.email}
                            </Button>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          {canComment && (
            <Button
              onClick={submit}
              disabled={!body.trim() || addComment.isPending}
              aria-busy={addComment.isPending}
              variant="default"
            >
              {addComment.isPending && (
                <Spinner data-icon="inline-start" aria-label={undefined} />
              )}
              {t("access:workspaces.comments.post")}
            </Button>
          )}
          <Button onClick={onClose} variant="link">
            {t("common:actions.close")}
          </Button>
        </DialogFooter>
        {editing && (
          <Dialog
            open={true}
            onOpenChange={(open) => {
              if (!open) (() => setEditing(null))();
            }}
          >
            <DialogContent
              aria-labelledby="meeting-comment-edit-title"
              aria-describedby={undefined}
              className="max-h-[90dvh] overflow-y-auto sm:max-w-lg"
            >
              <DialogHeader>
                <DialogTitle id="meeting-comment-edit-title">
                  {t("access:workspaces.comments.editTitle")}
                </DialogTitle>
              </DialogHeader>
              <div className="min-w-0">
                <div className="flex flex-col gap-4">
                  {editError && (
                    <div>
                      <Alert
                        variant="danger"
                        isInline={true}
                        isLiveRegion={true}
                        title={editError}
                      />
                    </div>
                  )}
                  <div>
                    <Textarea
                      value={editBody}
                      rows={3}
                      aria-label={t("access:workspaces.comments.bodyAria")}
                      onChange={(_e) => {
                        const value = _e.currentTarget.value;
                        return setEditBody(value);
                      }}
                    />
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button
                  onClick={() => {
                    setEditError(null);
                    updateComment.mutate(
                      { commentId: editing.id, body: editBody.trim() },
                      {
                        onSuccess: () => setEditing(null),
                        onError: (error) =>
                          setEditError(
                            getErrorMessage(error, t("comments.actionFailed")),
                          ),
                      },
                    );
                  }}
                  disabled={!editBody.trim() || updateComment.isPending}
                  aria-busy={updateComment.isPending}
                  variant="default"
                >
                  {updateComment.isPending && (
                    <Spinner data-icon="inline-start" aria-label={undefined} />
                  )}
                  {t("common:actions.save")}
                </Button>
                <Button onClick={() => setEditing(null)} variant="link">
                  {t("common:actions.cancel")}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        )}
      </DialogContent>
    </Dialog>
  );
}
