import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { BellIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Field, FieldLabel } from "@/components/ui/field";
import { getErrorMessage } from "@/api/client";
import type { AppNotification } from "@/api/types";
import { DataState } from "@/components/DataState";
import { useToast } from "@/components/toast/useToast";
import {
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotifications,
  useUnreadNotificationCount,
} from "@/hooks/useNotifications";
import { formatRelativeTime } from "@/utils/relativeTime";

export function NotificationBellToggle({
  isOpen,
  onToggle,
}: {
  isOpen: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation("common");
  const { data: unread = 0 } = useUnreadNotificationCount();
  return (
    <Button
      variant="ghost"
      size={unread ? "sm" : "icon-sm"}
      aria-expanded={isOpen}
      onClick={onToggle}
      aria-label={t("notificationsBell.toggleAria", { count: unread })}
    >
      <BellIcon data-icon="inline-start" />
      {unread > 0 && <Badge variant="secondary">{unread}</Badge>}
    </Button>
  );
}
export function NotificationsDrawerPanel({ onClose }: { onClose: () => void }) {
  const { t, i18n } = useTranslation("notifications");
  const navigate = useNavigate();
  const toast = useToast();
  const query = useNotifications();
  const markRead = useMarkNotificationRead();
  const markAllRead = useMarkAllNotificationsRead();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const notifications = (query.data ?? []).filter(
    (n) => !unreadOnly || n.read_at === null,
  );
  const open = (n: AppNotification) => {
    if (n.read_at === null)
      markRead.mutate(
        { id: n.id, read: true },
        {
          onError: (error) =>
            toast.error(t("markReadError"), getErrorMessage(error)),
        },
      );
    if (n.link) {
      navigate(n.link);
      onClose();
    }
  };
  return (
    <>
      <SheetHeader>
        <SheetTitle>{t("title")}</SheetTitle>
        <SheetDescription>
          {t(unreadOnly ? "filter.showingUnread" : "filter.showingAll")}
        </SheetDescription>
      </SheetHeader>
      <div className="flex flex-col gap-4 px-4 pb-4">
        <Field orientation="horizontal">
          <Switch
            id="notifications-unread"
            checked={unreadOnly}
            onCheckedChange={setUnreadOnly}
          />
          <FieldLabel htmlFor="notifications-unread">
            {t("filter.unreadOnly")}
          </FieldLabel>
        </Field>
        <Button
          variant="outline"
          size="sm"
          disabled={markAllRead.isPending}
          onClick={() =>
            markAllRead.mutate(undefined, {
              onError: (error) =>
                toast.error(t("markAllReadError"), getErrorMessage(error)),
            })
          }
        >
          {t("markAllRead")}
        </Button>
        <Separator />
        <DataState
          isLoading={query.isLoading}
          error={query.error}
          isEmpty={notifications.length === 0}
          emptyTitle={t("empty")}
          emptyBody={t("emptyBody")}
          emptyIcon={BellIcon}
          onRetry={() => void query.refetch()}
        >
          <ul aria-label={t("listAria")} className="flex flex-col gap-2">
            {notifications.map((n) => (
              <li key={n.id}>
                <Button
                  variant={n.read_at ? "ghost" : "secondary"}
                  className="h-auto w-full justify-start whitespace-normal py-3"
                  onClick={() => open(n)}
                >
                  <span className="flex min-w-0 flex-col gap-1 text-left">
                    <span>{n.title}</span>
                    <span className="text-sm font-normal text-muted-foreground">
                      {n.body}
                    </span>
                    <time className="text-xs font-normal text-muted-foreground">
                      {formatRelativeTime(n.created_at, i18n.language)}
                    </time>
                  </span>
                </Button>
              </li>
            ))}
          </ul>
        </DataState>
      </div>
    </>
  );
}
