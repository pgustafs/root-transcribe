/**
 * Client-side media preflight (issue #37).
 *
 * A cheap, structural sanity check on a recorded blob BEFORE it is uploaded, so
 * an obviously empty / truncated / wrong-container object is caught locally
 * (preserving the recoverable session) instead of being accepted by transport
 * and only failing asynchronously in the worker. This is NOT authoritative —
 * the server/worker media validation is (declared MIME is not trusted there) —
 * it just catches the obvious cases early with a clear, safe message.
 */

export type PreflightReason = "empty" | "too_small" | "bad_header";

export type PreflightResult =
  { ok: true } | { ok: false; reason: PreflightReason };

/**
 * Below this a blob cannot hold a real media header + any audio. Kept in step
 * with the authoritative server guard (`media_validation.MIN_MEDIA_BYTES`): a
 * lower client threshold would accept objects the worker then rejects, which is
 * exactly the asynchronous failure this preflight exists to prevent.
 */
export const MIN_RECORDING_BYTES = 256;

const EBML_MAGIC = [0x1a, 0x45, 0xdf, 0xa3]; // WebM / Matroska
const OGG_MAGIC = [0x4f, 0x67, 0x67, 0x53]; // "OggS"
const MP4_FTYP = [0x66, 0x74, 0x79, 0x70]; // "ftyp" (at offset 4)

type Container = "webm" | "ogg" | "mp4";

function startsWith(bytes: Uint8Array, magic: number[], offset = 0): boolean {
  return magic.every((b, i) => bytes[offset + i] === b);
}

/**
 * Which container we should hold the bytes to, from the declared MIME or — when
 * that is missing or generic (`""`, `application/octet-stream`, as file pickers
 * often report) — the filename extension. Without the fallback a malformed
 * `.webm` would skip header validation entirely (issue #37 review).
 */
export function resolveContainer(
  mimeType: string | undefined,
  filename?: string,
): Container | null {
  const mime = (mimeType ?? "").toLowerCase();
  if (mime.includes("webm") || mime.includes("matroska")) return "webm";
  if (mime.includes("ogg")) return "ogg";
  if (mime.includes("mp4") || mime.includes("m4a")) return "mp4";
  const ext = filename?.toLowerCase().match(/\.([a-z0-9]+)$/)?.[1];
  if (ext === "webm" || ext === "mkv") return "webm";
  if (ext === "ogg" || ext === "oga" || ext === "opus") return "ogg";
  if (ext === "mp4" || ext === "m4a") return "mp4";
  return null;
}

/**
 * Validate a recorded blob's size and container header. Returns `{ ok: true }`
 * or a stable reason the caller maps to localized copy. ``filename`` lets the
 * check still apply when the MIME is absent or generic.
 */
export async function preflightRecording(
  blob: Blob,
  mimeType: string,
  filename?: string,
): Promise<PreflightResult> {
  if (blob.size === 0) return { ok: false, reason: "empty" };
  if (blob.size < MIN_RECORDING_BYTES)
    return { ok: false, reason: "too_small" };

  const container = resolveContainer(mimeType, filename);
  if (!container) {
    // Genuinely unknown container (e.g. wav/mp3 the server also accepts):
    // size checks only — the worker's bounded decode is authoritative.
    return { ok: true };
  }
  const head = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
  const ok =
    container === "webm"
      ? startsWith(head, EBML_MAGIC)
      : container === "ogg"
        ? startsWith(head, OGG_MAGIC)
        : startsWith(head, MP4_FTYP, 4);
  return ok ? { ok: true } : { ok: false, reason: "bad_header" };
}
