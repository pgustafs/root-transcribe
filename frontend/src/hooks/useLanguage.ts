/**
 * Active UI language + a setter, mirroring `useTheme`: the choice is persisted to
 * localStorage and reflected on `<html lang>` so it survives reloads and stays
 * correct for assistive tech.
 */

import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import {
  LANG_STORAGE_KEY,
  type LanguageCode,
  SUPPORTED_LANGUAGES,
} from "@/i18n";

export function useLanguage() {
  const { i18n } = useTranslation();
  const language = i18n.language as LanguageCode;

  const setLanguage = useCallback(
    (next: LanguageCode) => {
      void i18n.changeLanguage(next);
      localStorage.setItem(LANG_STORAGE_KEY, next);
      document.documentElement.lang = next;
    },
    [i18n],
  );

  return { language, setLanguage, languages: SUPPORTED_LANGUAGES };
}
