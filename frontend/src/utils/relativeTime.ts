/** Locale-aware "how long ago" formatting for compact metadata bylines. */

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
/** Beyond a week, a date reads better than "9 days ago". */
const RELATIVE_CUTOFF = 7 * DAY;

/**
 * Format an ISO timestamp relative to now ("2 min ago", "3 days ago") in the given
 * locale, falling back to a short date ("5 Jun") beyond a week — and to a dated
 * year ("5 Jun 2025") once it's no longer this year.
 */
export function formatRelativeTime(
  iso: string,
  locale: string,
  now: Date = new Date(),
): string {
  const then = new Date(iso);
  const elapsed = now.getTime() - then.getTime();
  if (Number.isNaN(then.getTime())) return iso;

  if (elapsed < RELATIVE_CUTOFF) {
    const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    if (elapsed < MINUTE) return rtf.format(0, "minute");
    if (elapsed < HOUR)
      return rtf.format(-Math.floor(elapsed / MINUTE), "minute");
    if (elapsed < DAY) return rtf.format(-Math.floor(elapsed / HOUR), "hour");
    return rtf.format(-Math.floor(elapsed / DAY), "day");
  }
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    ...(then.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  }).format(then);
}
