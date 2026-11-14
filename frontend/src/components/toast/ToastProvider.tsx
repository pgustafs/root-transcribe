import { useMemo, useRef, type ReactNode } from "react";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import {
  ToastContext,
  type ToastContextValue,
  type ToastInput,
} from "@/components/toast/toastContext";
export function ToastProvider({ children }: { children: ReactNode }) {
  const nextId = useRef(1);
  const value = useMemo<ToastContextValue>(() => {
    const notify = (input: ToastInput) => {
      const method =
        input.variant === "danger"
          ? toast.error
          : input.variant === "success"
            ? toast.success
            : input.variant === "warning"
              ? toast.warning
              : toast.info;
      method(input.title, {
        id: nextId.current++,
        description: input.description,
        duration: input.variant === "danger" ? Infinity : 8000,
      });
    };
    return {
      notify,
      dismiss: (id) => toast.dismiss(id),
      success: (title, description) =>
        notify({ variant: "success", title, description }),
      error: (title, description) =>
        notify({ variant: "danger", title, description }),
    };
  }, []);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <Toaster closeButton position="bottom-right" />
    </ToastContext.Provider>
  );
}
