import axios, {
  AxiosError,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from "axios";

import { clearIdentity, getAccessToken } from "@/auth/keycloak";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export const apiClient = axios.create({
  baseURL: BASE_URL,
  headers: { "Content-Type": "application/json" },
});

// Allow the auth layer to react to an unrecoverable auth failure.
let onAuthFailure: (() => void) | null = null;
export function setOnAuthFailure(handler: (() => void) | null): void {
  onAuthFailure = handler;
}

apiClient.interceptors.request.use(
  async (config: InternalAxiosRequestConfig) => {
    try {
      const token = await getAccessToken();
      if (token) config.headers.Authorization = `Bearer ${token}`;
      return config;
    } catch (error) {
      clearIdentity();
      onAuthFailure?.();
      throw error;
    }
  },
);

type RetriableConfig = AxiosRequestConfig & { _retry?: boolean };
apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as RetriableConfig | undefined;
    if (error.response?.status !== 401 || !original) throw error;
    try {
      if (original._retry) throw error;
      original._retry = true;
      const token = await getAccessToken(true);
      if (!token) throw error;
      original.headers = {
        ...original.headers,
        Authorization: `Bearer ${token}`,
      };
      return await apiClient(original);
    } catch (refreshError) {
      clearIdentity();
      onAuthFailure?.();
      throw refreshError;
    }
  },
);

/** Extract a human-readable message from an unknown error (backend `detail`). */
export function getErrorMessage(
  error: unknown,
  fallback = "Request failed",
): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)
      ?.detail;
    if (typeof detail === "string") {
      return detail;
    }
    return error.message || fallback;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return fallback;
}
