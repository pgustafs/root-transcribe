/**
 * Presentation for every status enum the API exposes. UI code must never
 * render a raw enum value: import a map (or `StatusLabel`) so wording, casing
 * and status colours stay consistent across pages. Text is an i18n key
 * ("namespace:path"), resolved by `StatusLabel` / `useEnumLabel`.
 *
 * Add a map here when a second screen needs the same enum; a map used by one
 * screen may live next to that screen, built with the same helpers.
 */

import type {
  JobStatus,
  MeetingStatus,
  Role,
  WorkspaceRole,
} from "@/api/types";

/** Semantic status presentation shared by badges and activity indicators. */
export type StatusTone =
  | { status: "success" | "warning" | "danger" | "info" | "custom" }
  | { color: "grey" | "blue" | "teal" | "purple" | "orange" | "yellow" };

export type StatusPresentation = {
  /** i18n key, "namespace:path". */
  labelKey: string;
  tone: StatusTone;
  /** Shows a spinner: the state is transient and will change on its own. */
  inProgress?: boolean;
};

export const neutral = (labelKey: string): StatusPresentation => ({
  labelKey,
  tone: { color: "grey" },
});
export const info = (labelKey: string, inProgress = false) =>
  ({ labelKey, tone: { status: "info" }, inProgress }) as StatusPresentation;
export const success = (labelKey: string): StatusPresentation => ({
  labelKey,
  tone: { status: "success" },
});
export const warning = (labelKey: string): StatusPresentation => ({
  labelKey,
  tone: { status: "warning" },
});
export const danger = (labelKey: string): StatusPresentation => ({
  labelKey,
  tone: { status: "danger" },
});
export const tag = (
  labelKey: string,
  color: Extract<StatusTone, { color: string }>["color"],
): StatusPresentation => ({ labelKey, tone: { color } });

/** "delete_failed" → "Delete failed"; the fallback for an unmapped value. */
export function humanize(value: string): string {
  const spaced = value.replace(/[_-]+/g, " ").trim().toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Platform roles as words (not as status — they carry no state). */
export const ROLE_LABELS: Record<Role, string> = {
  admin: "common:role.admin",
  power_user: "common:role.power_user",
  feedback_reviewer: "common:role.feedback_reviewer",
  user: "common:role.user",
};

export const WORKSPACE_ROLE_LABELS: Record<WorkspaceRole, string> = {
  owner: "access:workspaces.roles.owner",
  manager: "access:workspaces.roles.manager",
  editor: "access:workspaces.roles.editor",
  viewer: "access:workspaces.roles.viewer",
};

export const JOB_STATUS: Record<JobStatus, StatusPresentation> = {
  pending: info("jobs:status.pending"),
  started: info("jobs:status.started", true),
  success: success("jobs:status.success"),
  failure: danger("jobs:status.failure"),
  retry: warning("jobs:status.retry"),
  revoked: neutral("jobs:status.revoked"),
};

export const MEETING_STATUS: Record<MeetingStatus, StatusPresentation> = {
  pending: info("meetings:status.pending", true),
  uploading: info("meetings:status.uploading", true),
  transcribing: info("meetings:status.transcribing", true),
  diarizing: info("meetings:status.diarizing", true),
  identifying: info("meetings:status.identifying", true),
  completed: success("meetings:status.completed"),
  failed: danger("meetings:status.failed"),
};

/** Active / inactive account or feature state. */
export const ACTIVE_STATUS: Record<"active" | "inactive", StatusPresentation> =
  {
    active: success("common:status.active"),
    inactive: neutral("common:status.inactive"),
  };
