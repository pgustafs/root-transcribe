import { useCallback, useEffect, useSyncExternalStore } from "react";

/** Same key as the pre-paint script in index.html. */
const STORAGE_KEY = "root_transcribe.theme";
const CHANGE_EVENT = "root-transcribe-theme";
export type ThemeChoice = "light" | "dark" | "system";
type Theme = "light" | "dark";

const media = () => window.matchMedia("(prefers-color-scheme: dark)");

function subscribe(notify: () => void) {
  const mq = media();
  window.addEventListener("storage", notify);
  window.addEventListener(CHANGE_EVENT, notify);
  mq.addEventListener("change", notify);
  return () => {
    window.removeEventListener("storage", notify);
    window.removeEventListener(CHANGE_EVENT, notify);
    mq.removeEventListener("change", notify);
  };
}
function readChoice(): ThemeChoice {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}
/** Encoded as "<choice>:<resolved>" so one snapshot carries both. */
function snapshot(): string {
  const choice = readChoice();
  const resolved: Theme =
    choice === "system" ? (media().matches ? "dark" : "light") : choice;
  return `${choice}:${resolved}`;
}

/**
 * Light / dark / system theme. Light = the enrooted "vit" theme, dark = the
 * "navy" theme; "system" follows the OS setting.
 */
export function useTheme() {
  const snap = useSyncExternalStore(subscribe, snapshot, () => "system:light");
  const [choice, theme] = snap.split(":") as [ThemeChoice, Theme];
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.style.colorScheme = theme;
  }, [theme]);
  const setTheme = useCallback((next: ThemeChoice) => {
    try {
      if (next === "system") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* storage blocked: the change still applies for this page view */
    }
    window.dispatchEvent(new Event(CHANGE_EVENT));
  }, []);
  const toggleTheme = useCallback(
    () => setTheme(theme === "dark" ? "light" : "dark"),
    [setTheme, theme],
  );
  return { choice, theme, isDark: theme === "dark", setTheme, toggleTheme };
}
