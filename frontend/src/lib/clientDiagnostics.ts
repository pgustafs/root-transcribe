/**
 * Privacy-safe client diagnostics for recording failures (issue #37).
 *
 * #37 requires recording the supported browser/version so a future
 * browser-specific invalid-container spike can be isolated. We report a CLOSED
 * family enum plus the major version — never the raw user agent. The server
 * writes these to its operator log, so a free-text value here would be a
 * log-injection/exfiltration channel rather than a diagnostic (review round 5).
 */

/** Families the backend accepts (`MeetingBrowserFamily`). Keep in sync. */
export const BROWSER_FAMILIES = [
  "Chrome",
  "Firefox",
  "Safari",
  "Edge",
  "Opera",
  "unknown",
] as const;

export type BrowserFamily = (typeof BROWSER_FAMILIES)[number];

export type BrowserInfo = {
  family: BrowserFamily;
  /** Major version only — enough to correlate with a browser release. */
  major: number | null;
};

/**
 * The browser family + major version, e.g. `{ family: "Chrome", major: 126 }`.
 * Anything unrecognized degrades to `{ family: "unknown", major: null }` — it
 * never passes an arbitrary string through.
 */
export function browserInfo(
  userAgent: string | undefined = typeof navigator !== "undefined"
    ? navigator.userAgent
    : undefined,
): BrowserInfo {
  const unknown: BrowserInfo = { family: "unknown", major: null };
  if (!userAgent) return unknown;
  // Order matters: Edge/Opera/Chrome all carry "Chrome" in their UA.
  const patterns: [BrowserFamily, RegExp][] = [
    ["Edge", /Edg(?:e|A|iOS)?\/(\d+)/],
    ["Opera", /OPR\/(\d+)/],
    ["Chrome", /Chrome\/(\d+)/],
    ["Firefox", /Firefox\/(\d+)/],
    ["Safari", /Version\/(\d+).*Safari/],
  ];
  for (const [family, re] of patterns) {
    const match = userAgent.match(re);
    if (match) {
      const major = Number.parseInt(match[1], 10);
      return {
        family,
        // The backend bounds this to 0..999; drop anything implausible rather
        // than have the whole diagnostic rejected.
        major:
          Number.isFinite(major) && major >= 0 && major <= 999 ? major : null,
      };
    }
  }
  return unknown;
}

/** Closed container/codec tokens the backend accepts. A MIME string is
 * caller-chosen text — `secret/topsecret` is MIME-shaped — so the client
 * normalises to these and anything unrecognised becomes "unknown"
 * (issue #37 review round 6). */
export const MEDIA_CONTAINERS = [
  "webm",
  "ogg",
  "mp4",
  "mp3",
  "wav",
  "flac",
  "aac",
  "unknown",
] as const;
export const MEDIA_CODECS = [
  "opus",
  "vorbis",
  "aac",
  "mp3",
  "pcm",
  "flac",
  "unknown",
] as const;

export type MediaContainer = (typeof MEDIA_CONTAINERS)[number];
export type MediaCodec = (typeof MEDIA_CODECS)[number];

/** Normalise a MediaRecorder/File MIME (plus optional filename) into the closed
 * container + codec tokens. Never passes the original string through. */
export function mediaTokens(
  mimeType: string | null | undefined,
  filename?: string,
): { container: MediaContainer; codec: MediaCodec } {
  const mime = (mimeType ?? "").toLowerCase();
  const ext = filename?.toLowerCase().match(/\.([a-z0-9]+)$/)?.[1] ?? "";

  const container: MediaContainer =
    mime.includes("webm") || mime.includes("matroska") || ext === "webm"
      ? "webm"
      : mime.includes("ogg") || ext === "ogg" || ext === "oga" || ext === "opus"
        ? "ogg"
        : mime.includes("mp4") ||
            mime.includes("m4a") ||
            ext === "mp4" ||
            ext === "m4a"
          ? "mp4"
          : mime.includes("mpeg") || mime.includes("mp3") || ext === "mp3"
            ? "mp3"
            : mime.includes("wav") || ext === "wav"
              ? "wav"
              : mime.includes("flac") || ext === "flac"
                ? "flac"
                : mime.includes("aac") || ext === "aac"
                  ? "aac"
                  : "unknown";

  const codec: MediaCodec = mime.includes("opus")
    ? "opus"
    : mime.includes("vorbis")
      ? "vorbis"
      : mime.includes("aac") || mime.includes("mp4a")
        ? "aac"
        : mime.includes("mp3") || mime.includes("mpeg")
          ? "mp3"
          : mime.includes("pcm")
            ? "pcm"
            : mime.includes("flac")
              ? "flac"
              : "unknown";
  return { container, codec };
}
