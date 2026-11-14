import { createContext } from "react";

export type ToastVariant = "success" | "danger" | "warning" | "info";

export type Toast = {
  id: number;
  variant: ToastVariant;
  title: string;
  description?: string;
};

export type ToastInput = Omit<Toast, "id">;

export type ToastContextValue = {
  notify: (input: ToastInput) => void;
  success: (title: string, description?: string) => void;
  error: (title: string, description?: string) => void;
  dismiss: (id: number) => void;
};

export const ToastContext = createContext<ToastContextValue | undefined>(
  undefined,
);
