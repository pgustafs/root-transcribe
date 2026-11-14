/**
 * i18next bootstrap. Initializes react-i18next with statically-bundled `en`/`sv`
 * catalogs (one namespace per area). The active language is persisted in
 * localStorage and mirrored onto `<html lang>` — same pattern as `useTheme`.
 *
 * Import this once for its side effect (`import "@/i18n"` in `main.tsx` and the
 * test harness); components use `useTranslation()` / `useLanguage()`.
 */

import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enAccess from "@/locales/en/access.json";
import enAuth from "@/locales/en/auth.json";
import enCommon from "@/locales/en/common.json";
import enJobs from "@/locales/en/jobs.json";
import enMeetings from "@/locales/en/meetings.json";
import enNotes from "@/locales/en/notes.json";
import enNotifications from "@/locales/en/notifications.json";
import enUsers from "@/locales/en/users.json";
import enValidation from "@/locales/en/validation.json";
import svAccess from "@/locales/sv/access.json";
import svAuth from "@/locales/sv/auth.json";
import svCommon from "@/locales/sv/common.json";
import svJobs from "@/locales/sv/jobs.json";
import svMeetings from "@/locales/sv/meetings.json";
import svNotes from "@/locales/sv/notes.json";
import svNotifications from "@/locales/sv/notifications.json";
import svUsers from "@/locales/sv/users.json";
import svValidation from "@/locales/sv/validation.json";

/** Languages offered in the UI. Labels are endonyms (not translated). */
export const SUPPORTED_LANGUAGES = [
  { code: "en", label: "English" },
  { code: "sv", label: "Svenska" },
] as const;

export type LanguageCode = (typeof SUPPORTED_LANGUAGES)[number]["code"];

export const LANG_STORAGE_KEY = "root_transcribe.lang";

const NAMESPACES = [
  "access",
  "common",
  "auth",
  "users",
  "jobs",
  "notifications",
  "meetings",
  "notes",
  "validation",
] as const;

const resources = {
  en: {
    common: enCommon,
    access: enAccess,
    auth: enAuth,
    users: enUsers,
    jobs: enJobs,
    notifications: enNotifications,
    meetings: enMeetings,
    notes: enNotes,
    validation: enValidation,
  },
  sv: {
    common: svCommon,
    access: svAccess,
    auth: svAuth,
    users: svUsers,
    jobs: svJobs,
    notifications: svNotifications,
    meetings: svMeetings,
    notes: svNotes,
    validation: svValidation,
  },
} as const;

function initialLanguage(): LanguageCode {
  const stored = localStorage.getItem(LANG_STORAGE_KEY);
  return SUPPORTED_LANGUAGES.some((l) => l.code === stored)
    ? (stored as LanguageCode)
    : "en";
}

const lng = initialLanguage();

void i18n.use(initReactI18next).init({
  resources,
  lng,
  fallbackLng: "en",
  ns: NAMESPACES,
  defaultNS: "common",
  interpolation: { escapeValue: false }, // React already escapes
  returnNull: false,
});

if (typeof document !== "undefined") {
  document.documentElement.lang = lng;
}

export default i18n;
