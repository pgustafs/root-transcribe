/**
 * Sets the document title to "<page> · <product name>" so every route has a
 * distinct title (WCAG 2.4.2). The product name comes from backend config.
 */

import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { useConfig } from "@/hooks/useConfig";

export function usePageTitle(title: string | undefined) {
  const { t } = useTranslation("common");
  const { data: config } = useConfig();
  const appName = config?.project_name ?? t("app.fallbackName");
  useEffect(() => {
    document.title = title ? `${title} · ${appName}` : appName;
  }, [title, appName]);
}
