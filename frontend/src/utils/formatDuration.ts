/** The one shared `m:ss` duration formatter (previously copy-pasted per
 * feature, and one copy grew a rounding bug: `Math.round` on the seconds
 * remainder rendered 119.6s as "1:60"). Floors throughout — a duration is
 * "1:59" until it actually reaches 2:00. */
export function formatDuration(totalSeconds: number): string {
  const total = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** Zero-padded `mm:ss` from milliseconds (timestamps in transcripts,
 * where fixed width keeps columns of times aligned). */
export function formatMsClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(
    total % 60,
  ).padStart(2, "0")}`;
}
