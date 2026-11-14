import { useContext } from "react";

import { ToastContext } from "@/components/toast/toastContext";

/** Toast feedback for the outcome of a user action (created / deleted /
 * failed). Named `useToast` because `useNotifications` is the notification
 * bell's inbox query. */
export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used within ToastProvider");
  }
  return ctx;
}
