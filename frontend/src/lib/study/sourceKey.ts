/**
 * Which source a pasted link points at, so one video gets one notebook.
 * Mirrors backend/app/services/study_source.py (the server decides; this copy
 * lets the dialog say "this video already has a notebook" before saving).
 */

const YOUTUBE_HOSTS = new Set([
  "youtube.com",
  "www.youtube.com",
  "m.youtube.com",
  "music.youtube.com",
  "youtube-nocookie.com",
  "www.youtube-nocookie.com",
]);
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const PATH_PREFIXES = new Set(["shorts", "embed", "live", "v"]);
// "mailto:x", "javascript:…": a scheme without "//". "host:8080/x" is not one.
const BARE_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)/;

export function withScheme(url: string): string {
  const text = url.trim();
  if (text.includes("://") || BARE_SCHEME.test(text)) return text;
  return `https://${text}`;
}

function parse(url: string): URL | null {
  try {
    return new URL(withScheme(url));
  } catch {
    return null;
  }
}

export function isHttpUrl(url: string): boolean {
  const parsed = parse(url);
  return (
    parsed !== null &&
    (parsed.protocol === "http:" || parsed.protocol === "https:") &&
    parsed.hostname.includes(".")
  );
}

export function youtubeId(url: string): string | null {
  const parsed = parse(url);
  if (!parsed) return null;
  const host = parsed.hostname.toLowerCase();
  const segments = parsed.pathname.split("/").filter(Boolean);
  let candidate: string | null = null;
  if (host === "youtu.be" || host === "www.youtu.be") {
    candidate = segments[0] ?? null;
  } else if (YOUTUBE_HOSTS.has(host)) {
    if (segments[0] === "watch") candidate = parsed.searchParams.get("v");
    else if (segments.length >= 2 && PATH_PREFIXES.has(segments[0]))
      candidate = segments[1];
  }
  return candidate && VIDEO_ID.test(candidate) ? candidate : null;
}

/** Stable identity for grouping, or null when the link has none. */
export function sourceKey(url: string | null | undefined): string | null {
  if (!url || !url.trim()) return null;
  const video = youtubeId(url);
  return video ? `youtube:${video}` : null;
}
