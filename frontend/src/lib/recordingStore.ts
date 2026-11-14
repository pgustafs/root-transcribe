import type { RecordingLanguage } from "@/api/types";
/**
 * IndexedDB persistence for in-progress meeting recordings (issue #6, wave B4).
 *
 * Pre-#6, MediaRecorder chunks lived only in a React ref — a tab crash, reload,
 * or sleep lost the whole meeting. Every chunk now lands here as it arrives, and
 * upload state (session id, part checksums) is persisted too, so recording AND
 * upload recover after reload: the RecordingProvider hydrates stored sessions on
 * mount and offers resume/discard. Local data expires after a policy window so
 * discarded-but-forgotten audio doesn't sit in the browser forever.
 */

import { type DBSchema, type IDBPDatabase, openDB } from "idb";

export type StoredRecordingState =
  | "recording"
  | "pending-upload"
  | "uploading"
  /**
   * Uploaded (transport accepted, HTTP 202) but the worker has NOT yet
   * validated the media (issue #37). The local copy is deliberately KEPT in
   * this state — transport success is not proof of media validity, so a later
   * `recording_invalid_container` must not leave the user with nothing.
   */
  | "awaiting-validation";

export type StoredRecordingSession = {
  id: string;
  workspaceId: string;
  title: string;
  consent: boolean;
  requestedLanguage?: RecordingLanguage;
  mimeType: string;
  state: StoredRecordingState;
  /** Resumable-upload bookkeeping (set once the upload session opens). */
  uploadSessionId: string | null;
  recordingId: string | null;
  partSizeBytes: number | null;
  /** part number -> sha256 of every part already accepted by the server. */
  uploadedParts: Record<number, string>;
  /**
   * How many chunks the recorder has EMITTED (highest seq + 1). Written BEFORE
   * the chunk's data (intent-first, issue #37 review) so a failed or lost data
   * write still leaves evidence the chunk existed — otherwise losing the FINAL
   * chunk (or every chunk) would look like a complete recording.
   */
  expectedChunks: number;
  /** Sequences written more than once — a recorder/timing anomaly worth
   * refusing rather than silently overwriting. */
  duplicateSeqs: number[];
  /**
   * The browser that PRODUCED this recording (family + major only). Captured at
   * record time and reported with later rejection/recovery events: re-reading
   * the current browser then would attribute the failure to the wrong version
   * after an update or a delayed recovery (issue #37 review round 6).
   */
  clientFamily: string | null;
  clientMajor: number | null;
  createdAt: number;
  updatedAt: number;
};

/** Per-chunk timing metadata (issue #37): enough to diagnose an assembly bug
 * without recording any audio content. */
export type StoredChunk = {
  sessionId: string;
  seq: number;
  // Chunks are stored as ArrayBuffers: Blob structured-cloning is flaky
  // across IndexedDB implementations, raw buffers round-trip everywhere.
  data: ArrayBuffer;
  /** Wall-clock ms when the timeslice landed. */
  recordedAt: number;
  /** MediaRecorder's own timecode for the slice, when the browser provides it. */
  timecode: number | null;
  size: number;
};

interface RecordingDB extends DBSchema {
  sessions: { key: string; value: StoredRecordingSession };
  chunks: {
    key: [string, number];
    value: StoredChunk;
    indexes: { bySession: string };
  };
}

/** Abandoned local recordings are purged after this long (privacy policy). */
export const LOCAL_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

const DB_NAME = "meeting-recordings";

let dbPromise: Promise<IDBPDatabase<RecordingDB>> | null = null;

/** IndexedDB is unavailable in some contexts (private mode quirks, old jsdom);
 * every API degrades to a no-op so recording still works without recovery. */
export function isRecordingStoreAvailable(): boolean {
  return typeof indexedDB !== "undefined";
}

/** Thrown when another tab holds an older version of the database open and the
 * version upgrade therefore cannot proceed.
 *
 * IndexedDB does NOT fail this case: `open` simply waits, forever, for the
 * other connection to close. Without this, the first thing a recording does —
 * create its local session — hung silently before the microphone was ever
 * requested, so Start spun with no error and no way out (found in manual pod
 * testing after FE-C shipped). Callers turn this into actionable guidance.
 */
export class RecordingStoreUnavailableError extends Error {
  name = "RecordingStoreUnavailableError";

  constructor(
    message = "The local recording store is unavailable",
    readonly reason?: unknown,
  ) {
    super(message);
  }
}

/** The database is at a NEWER version than this build understands: another tab
 * running a newer deploy upgraded it. Retrying can never help — this tab has
 * to reload to get the code that knows the new schema (issue #37 r22). */
export class RecordingStoreOutdatedError extends RecordingStoreUnavailableError {
  override name = "RecordingStoreOutdatedError" as const;

  constructor() {
    super("This tab is older than the local recording database");
  }
}

export class RecordingStoreBlockedError extends RecordingStoreUnavailableError {
  override name = "RecordingStoreBlockedError" as const;

  constructor() {
    super("The local recording database is locked by another tab");
  }
}

/** How long a blocked upgrade waits for the other tab to yield before failing.
 * Any tab running THIS build closes its connection on `blocking` within a
 * tick, so the grace period only has to cover that hand-off; a tab running an
 * OLDER build never will, and the user needs to be told rather than left
 * watching a spinner. */
const UPGRADE_BLOCKED_GRACE_MS = 1500;

/** Hard ceiling on ANY open, blocked event or not.
 *
 * The `blocked` callback is not guaranteed: a second open request queued
 * behind the same holder may never fire it, and then a give-up armed only from
 * that callback waits forever — the original hang, one layer down. The store
 * must fail loudly whenever it cannot open, whatever the reason.
 */
const OPEN_TIMEOUT_MS = 6000;

/** How many captures are currently writing chunks.
 *
 * While this is non-zero the store REFUSES to yield its connection to another
 * tab's upgrade: closing it would silently disable the crash-persistence the
 * whole feature rests on, and the recorder would keep running with nothing
 * durable behind it (issue #37 review round 20). The other tab is told what to
 * do instead — an in-progress meeting outranks a background upgrade.
 */
let liveCaptures = 0;

/** Set when a `versionchange` arrived while a capture held the connection. The
 * upgrade is DEFERRED, not cancelled: the other tab is still waiting, so the
 * connection must close the moment the last capture ends — otherwise it blocks
 * that tab forever and only a manual reset would clear it (issue #37 r21). */
let deferredVersionChange = false;

/** Register a live capture; call the returned function when it stops. */
export function beginLiveCapture(): () => void {
  liveCaptures += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    liveCaptures = Math.max(0, liveCaptures - 1);
    if (liveCaptures > 0 || !deferredVersionChange) return;
    // Honour the upgrade we made the other tab wait for.
    deferredVersionChange = false;
    const closing = dbPromise;
    dbPromise = null;
    void closing?.then((database) => database.close()).catch(() => {});
  };
}

/** Notified when a WRITE fails. Writes must never throw into the recorder
 * callback — dropping capture would be worse — but they must never fail
 * SILENTLY either: the durable copy is the user's only safety net, so the
 * provider escalates this to a visible degraded state. */
type StorageFailureListener = (error: unknown) => void;
let storageFailureListener: StorageFailureListener | null = null;

export function setStorageFailureListener(
  listener: StorageFailureListener | null,
): void {
  storageFailureListener = listener;
}

function reportWriteFailure(error: unknown): void {
  storageFailureListener?.(error);
}

/** Sentinel for "the upgrade is blocked". Deliberately a RESOLUTION, not a
 * rejection: the blocked signal fires on a timer that can outlive every
 * consumer, and an abandoned rejected promise surfaces as an unhandled
 * rejection. Only `dbPromise` — which always has a handler attached — rejects.
 */
const BLOCKED = Symbol("recording-store-blocked");

function db(): Promise<IDBPDatabase<RecordingDB>> {
  if (!dbPromise) {
    let signalBlocked: (() => void) | null = null;
    let blockedTimer: ReturnType<typeof setTimeout> | undefined;
    const blocked = new Promise<typeof BLOCKED>((resolve) => {
      signalBlocked = () => resolve(BLOCKED);
    });
    // Armed unconditionally; `blocked` only SHORTENS it.
    blockedTimer = setTimeout(() => signalBlocked?.(), OPEN_TIMEOUT_MS);
    // v2 (issue #37) adds chunk-accounting + diagnostics fields. They are all
    // additive and read defensively, so no data migration is needed — the
    // guards below just make the upgrade safe on an existing v1 database.
    const opening = openDB<RecordingDB>(DB_NAME, 2, {
      upgrade(database) {
        if (!database.objectStoreNames.contains("sessions")) {
          database.createObjectStore("sessions", { keyPath: "id" });
        }
        if (!database.objectStoreNames.contains("chunks")) {
          const chunks = database.createObjectStore("chunks", {
            keyPath: ["sessionId", "seq"],
          });
          chunks.createIndex("bySession", "sessionId");
        }
      },
      /** ANOTHER tab wants a newer version and we are in its way: get out of
       * it. Closing on `versionchange` is what lets a multi-tab upgrade
       * complete at all — without it every tab waits for every other one. */
      blocking(_currentVersion, _blockedVersion, event) {
        // A live capture keeps the connection: yielding here would leave the
        // recorder running with no durable copy behind it, losing the meeting
        // on a crash with no warning (issue #37 review round 20). The blocked
        // tab surfaces actionable guidance after its grace period instead.
        if (liveCaptures > 0) {
          // Remember it: the release below performs the close we are deferring.
          deferredVersionChange = true;
          return;
        }
        (event.target as IDBDatabase | null)?.close();
        dbPromise = null;
      },
      /** OUR upgrade is blocked by an older connection elsewhere. Give it a
       * moment to close (any tab on this build will), then give up. */
      blocked() {
        // We now KNOW an older connection is in the way: any tab on this build
        // yields within a tick, so stop waiting much sooner than the ceiling.
        clearTimeout(blockedTimer);
        blockedTimer = setTimeout(
          () => signalBlocked?.(),
          UPGRADE_BLOCKED_GRACE_MS,
        );
      },
      /** The browser dropped the connection (tab discarded, storage reset) —
       * forget it so the next call opens a fresh one instead of reusing a
       * dead handle. */
      terminated() {
        dbPromise = null;
      },
    });
    let gaveUp = false;
    // The underlying open request keeps WAITING after we stop caring — if the
    // blocker eventually yields, that stray connection would hold the database
    // open (and block a later delete). Close it the moment it arrives.
    void opening
      .then((database) => {
        if (gaveUp) database.close();
      })
      .catch(() => {});
    // A VersionError means the stored database is NEWER than this build — a
    // different problem with different advice, so it gets its own type.
    const classified = opening.catch((error: unknown) => {
      throw (error as { name?: string })?.name === "VersionError"
        ? new RecordingStoreOutdatedError()
        : error;
    });
    void classified.catch(() => {});
    dbPromise = Promise.race([classified, blocked]).then((result) => {
      clearTimeout(blockedTimer);
      if (result === BLOCKED) {
        gaveUp = true;
        // Never cache a failed open: the blocking tab may close a second
        // later, and the next attempt must be allowed to succeed.
        dbPromise = null;
        throw new RecordingStoreBlockedError();
      }
      return result;
    });
    // Callers still receive the rejection from their own `await db()`; this
    // only stops an abandoned copy from surfacing as an unhandled rejection.
    void dbPromise.catch(() => {});
  }
  return dbPromise;
}

/** The database, or ``null`` when it cannot be opened right now.
 *
 * Read and maintenance paths use this: a database locked by another tab must
 * degrade to "nothing recoverable to show" — the same way this module already
 * degrades when IndexedDB is absent entirely — rather than reject out of a
 * background effect nobody is awaiting. Paths where the user is WAITING on the
 * result (creating a session for a new recording) call `db()` directly, so the
 * failure can be explained to them (issue #37).
 */
/** Run a READ, converting any storage failure into a typed error.
 *
 * Reads must NEVER degrade to "absent" (issue #37 review round 20): callers
 * decide destructively on emptiness — cancellation treats "no stored session"
 * as "nothing to cancel server-side", recovery treats "no chunks" as "nothing
 * was recorded and this can be deleted". Collapsing "could not look" into
 * either of those loses the user's audio. Storage-unavailable is its own
 * answer, and every fail-closed consumer branches on it.
 */
async function read<T>(
  operation: (database: IDBPDatabase<RecordingDB>) => Promise<T>,
): Promise<T> {
  if (!isRecordingStoreAvailable()) {
    throw new RecordingStoreUnavailableError(
      "IndexedDB is not available in this context",
    );
  }
  const database = await db();
  try {
    return await operation(database);
  } catch (error) {
    // Covers more than a locked database: our own `blocking` handler closes
    // the connection when another tab upgrades, so an operation already in
    // flight can find its handle gone.
    throw new RecordingStoreUnavailableError(undefined, error);
  }
}

/** A write from a RECORDER CALLBACK. Never throws — breaking capture would be
 * worse than a failed write — but never fails silently either: the failure is
 * escalated so the user learns their durable copy has stopped updating.
 *
 * ONLY for the per-chunk path. Lifecycle bookkeeping (server ids, accepted
 * parts, state transitions) uses `mutate` instead: the upload's
 * crash-consistency depends on those having actually landed, so a swallowed
 * failure there reopens the duplicate/stranded-upload windows this feature
 * spent several rounds closing (issue #37 review round 21).
 */
async function bestEffortWrite(
  operation: (database: IDBPDatabase<RecordingDB>) => Promise<void>,
): Promise<void> {
  if (!isRecordingStoreAvailable()) return;
  try {
    await operation(await db());
  } catch (error) {
    reportWriteFailure(error);
  }
}

/** A write whose success the caller DEPENDS on — it throws like a read.
 * `uploadRecording` awaits these before sending the next part precisely so a
 * failure stops the upload rather than proceeding with unpersisted ids. */
async function mutate(
  operation: (database: IDBPDatabase<RecordingDB>) => Promise<void>,
): Promise<void> {
  return read(operation);
}

/** A read whose emptiness is NOT acted on destructively (diagnostics, the
 * retention sweep): degrading keeps background work from throwing into
 * floating promises. Never use this where absence drives a delete. */
async function readOrDefault<T>(
  operation: (database: IDBPDatabase<RecordingDB>) => Promise<T>,
  fallback: T,
): Promise<T> {
  try {
    return await read(operation);
  } catch {
    return fallback;
  }
}

export async function createStoredSession(meta: {
  workspaceId: string;
  title: string;
  consent: boolean;
  requestedLanguage?: RecordingLanguage;
  mimeType: string;
  clientFamily?: string | null;
  clientMajor?: number | null;
}): Promise<string> {
  const id = crypto.randomUUID();
  if (!isRecordingStoreAvailable()) return id;
  const now = Date.now();
  await (
    await db()
  ).put("sessions", {
    id,
    workspaceId: meta.workspaceId,
    title: meta.title,
    consent: meta.consent,
    requestedLanguage: meta.requestedLanguage ?? "auto",
    mimeType: meta.mimeType,
    clientFamily: meta.clientFamily ?? null,
    clientMajor: meta.clientMajor ?? null,
    state: "recording",
    uploadSessionId: null,
    recordingId: null,
    partSizeBytes: null,
    uploadedParts: {},
    expectedChunks: 0,
    duplicateSeqs: [],
    createdAt: now,
    updatedAt: now,
  });
  return id;
}

/**
 * Record that the recorder EMITTED chunk ``seq``, before its data is written
 * (issue #37 review). Intent-first: if the data write then fails or the tab
 * dies mid-write, the expectation survives and assembleRecording reports the
 * gap instead of presenting a truncated recording as complete.
 */
export async function noteExpectedChunk(
  sessionId: string,
  seq: number,
): Promise<void> {
  return bestEffortWrite(async (database) => {
    const tx = database.transaction("sessions", "readwrite");
    const existing = await tx.store.get(sessionId);
    if (existing) {
      await tx.store.put({
        ...existing,
        expectedChunks: Math.max(existing.expectedChunks ?? 0, seq + 1),
        updatedAt: Date.now(),
      });
    }
    await tx.done;
  });
}

export async function appendChunk(
  sessionId: string,
  seq: number,
  blob: Blob,
  meta: { recordedAt?: number; timecode?: number | null } = {},
): Promise<void> {
  if (!isRecordingStoreAvailable()) return;
  // Read the bytes BEFORE opening the transaction — awaiting anything other
  // than an IDB request inside a transaction lets it auto-close.
  const data = await blob.arrayBuffer();
  return bestEffortWrite(async (database) => {
    const tx = database.transaction(["chunks", "sessions"], "readwrite");
    const chunks = tx.objectStore("chunks");
    const duplicate = (await chunks.get([sessionId, seq])) !== undefined;
    await chunks.put({
      sessionId,
      seq,
      data,
      recordedAt: meta.recordedAt ?? Date.now(),
      timecode: meta.timecode ?? null,
      size: data.byteLength,
    });
    if (duplicate) {
      // put() would silently overwrite, so record the anomaly for assembly to
      // refuse on (issue #37 review: duplicates must be DETECTED, not absorbed).
      const sessions = tx.objectStore("sessions");
      const session = await sessions.get(sessionId);
      if (session && !(session.duplicateSeqs ?? []).includes(seq)) {
        await sessions.put({
          ...session,
          duplicateSeqs: [...(session.duplicateSeqs ?? []), seq],
          updatedAt: Date.now(),
        });
      }
    }
    await tx.done;
  });
}

export async function updateStoredSession(
  id: string,
  patch: Partial<Omit<StoredRecordingSession, "id" | "createdAt">>,
): Promise<void> {
  return mutate(async (database) => {
    // Read-modify-write in ONE transaction: a separate get()+put() can resurrect
    // a session that a concurrent delete removed in between (IDB serializes
    // whole transactions on the store, so inside one tx the read is consistent).
    const tx = database.transaction("sessions", "readwrite");
    const existing = await tx.store.get(id);
    if (existing) {
      await tx.store.put({ ...existing, ...patch, updatedAt: Date.now() });
    }
    await tx.done;
  });
}

/** Record one server-accepted upload part (transactional merge — the upload
 * loop fires these without awaiting, so they must never clobber or resurrect
 * concurrent session writes). */
export async function recordUploadedPart(
  id: string,
  partNumber: number,
  sha256: string,
): Promise<void> {
  return mutate(async (database) => {
    const tx = database.transaction("sessions", "readwrite");
    const existing = await tx.store.get(id);
    if (existing) {
      await tx.store.put({
        ...existing,
        uploadedParts: { ...existing.uploadedParts, [partNumber]: sha256 },
        updatedAt: Date.now(),
      });
    }
    await tx.done;
  });
}

export async function listStoredSessions(): Promise<StoredRecordingSession[]> {
  return read(async (database) => {
    const rows = await database.getAll("sessions");
    return rows.sort((a, b) => b.createdAt - a.createdAt);
  });
}

/** THROWS `RecordingStoreUnavailableError` when storage cannot be consulted.
 * `undefined` means the session genuinely is not there — callers that delete
 * or skip server cleanup on that basis must be able to tell the two apart. */
export async function getStoredSession(
  id: string,
): Promise<StoredRecordingSession | undefined> {
  return read((database) => database.get("sessions", id));
}

/** A chunk's diagnostics WITHOUT its audio bytes (issue #37): timing/size only,
 * so callers can reason about assembly without touching content. */
export type StoredChunkMeta = Omit<StoredChunk, "data">;

/** Per-chunk timing metadata for a session, ordered by sequence. */
export async function listChunkMeta(
  sessionId: string,
): Promise<StoredChunkMeta[]> {
  return readOrDefault(async (database) => {
    const rows = await database.getAllFromIndex(
      "chunks",
      "bySession",
      sessionId,
    );
    return rows
      .sort((a, b) => a.seq - b.seq)
      .map(({ sessionId: sid, seq, recordedAt, timecode, size }) => ({
        sessionId: sid,
        seq,
        recordedAt,
        timecode,
        size,
      }));
  }, []);
}

/**
 * Bounded, privacy-safe assembly diagnostics for one session (issue #37 review
 * round 5). Aggregates only — enough for an operator to answer "did the browser
 * emit badly, or did we assemble badly?" without any per-chunk data leaving the
 * device: the largest gap between consecutive timeslices exposes a stalled
 * recorder, while missing/duplicate counts point at local persistence instead.
 */
export type AssemblyDiagnostics = {
  chunk_count: number;
  total_bytes: number;
  /**
   * Largest WALL-CLOCK interval (ms) between consecutive timeslices — includes
   * any delay in our own callback/persistence path. Null if <2 chunks.
   */
  max_gap_ms: number | null;
  /**
   * Largest MEDIA-TIME interval, from the recorder's own `timecode`: a stalled
   * RECORDER. Null when the browser provides no timecodes.
   */
  max_media_gap_ms: number | null;
  /**
   * The largest |wall − media| difference across the same pairs — how far our
   * handling lagged the recorder. Comparing the three separates "the browser
   * emitted badly" from "we persisted slowly" (issue #37 review round 6).
   */
  max_skew_ms: number | null;
};

export async function assemblyDiagnostics(
  sessionId: string,
): Promise<AssemblyDiagnostics> {
  const rows = await listChunkMeta(sessionId);
  let maxGap: number | null = null;
  let maxMediaGap: number | null = null;
  let maxSkew: number | null = null;
  const better = (current: number | null, candidate: number) =>
    Number.isFinite(candidate) &&
    candidate >= 0 &&
    (current === null || candidate > current);

  const usable = (gap: number) => Number.isFinite(gap) && gap >= 0;
  for (let i = 1; i < rows.length; i += 1) {
    const wallGap = rows[i].recordedAt - rows[i - 1].recordedAt;
    const wallOk = usable(wallGap);
    if (wallOk && better(maxGap, wallGap)) maxGap = wallGap;

    const prevCode = rows[i - 1].timecode;
    const code = rows[i].timecode;
    if (prevCode === null || code === null) continue;
    const mediaGap = code - prevCode;
    const mediaOk = usable(mediaGap);
    if (mediaOk && better(maxMediaGap, mediaGap)) maxMediaGap = mediaGap;
    // Skew is only meaningful when BOTH components are themselves valid: a
    // backward wall clock or a non-monotonic timecode would otherwise
    // manufacture a huge phantom "persistence lag" (issue #37 review r7).
    if (wallOk && mediaOk) {
      const skew = Math.abs(wallGap - mediaGap);
      if (better(maxSkew, skew)) maxSkew = skew;
    }
  }
  const round = (v: number | null) => (v === null ? null : Math.round(v));
  return {
    chunk_count: rows.length,
    total_bytes: rows.reduce((sum, r) => sum + (r.size ?? 0), 0),
    max_gap_ms: round(maxGap),
    max_media_gap_ms: round(maxMediaGap),
    max_skew_ms: round(maxSkew),
  };
}

export type AssembledRecording = {
  blob: Blob;
  /** Number of persisted chunks found. */
  chunkCount: number;
  /**
   * Sequences the recorder emitted but that are NOT in the store (issue #37).
   * The expected range comes from the session's `expectedChunks` (recorded
   * before each data write), NOT from the highest stored sequence — otherwise
   * losing the final chunk, or every chunk, would look like a complete
   * recording. A non-empty list means the assembled object is KNOWN-incomplete
   * and must not be uploaded.
   */
  missing: number[];
  /** Sequences that were written more than once (recorder/timing anomaly). */
  duplicates: number[];
};

/**
 * Reassemble a recording from its persisted chunks, STRICTLY ordered by
 * validated sequence, reporting gaps and duplicate writes (issue #37).
 */
export async function assembleRecording(
  sessionId: string,
  mimeType: string,
): Promise<AssembledRecording> {
  if (!isRecordingStoreAvailable()) {
    return {
      blob: new Blob([], { type: mimeType }),
      chunkCount: 0,
      missing: [],
      duplicates: [],
    };
  }
  return read(async (database) => {
    const rows = await database.getAllFromIndex(
      "chunks",
      "bySession",
      sessionId,
    );
    const session = await database.get("sessions", sessionId);
    rows.sort((a, b) => a.seq - b.seq);
    const present = new Set(rows.map((r) => r.seq));
    // Expect whatever the recorder said it emitted; fall back to the stored high
    // water mark for legacy (pre-#37) sessions that predate the accounting.
    const highest = rows.length > 0 ? rows[rows.length - 1].seq + 1 : 0;
    const expected = Math.max(session?.expectedChunks ?? 0, highest);
    const missing: number[] = [];
    for (let seq = 0; seq < expected; seq += 1) {
      if (!present.has(seq)) missing.push(seq);
    }
    return {
      blob: new Blob(
        rows.map((r) => r.data),
        { type: mimeType },
      ),
      chunkCount: rows.length,
      missing,
      duplicates: session?.duplicateSeqs ?? [],
    };
  });
}

/** THROWS when storage cannot be consulted: a silent no-op would tell the user
 * their recording was discarded when it is still on disk. */
export async function deleteStoredSession(id: string): Promise<void> {
  return read(async (database) => {
    const tx = database.transaction(["sessions", "chunks"], "readwrite");
    await tx.objectStore("sessions").delete(id);
    const chunkStore = tx.objectStore("chunks");
    for (const key of await chunkStore.index("bySession").getAllKeys(id)) {
      await chunkStore.delete(key);
    }
    await tx.done;
  });
}

/** Drop local sessions older than the retention window. */
export async function purgeExpiredSessions(
  maxAgeMs: number = LOCAL_RETENTION_MS,
): Promise<number> {
  if (!isRecordingStoreAvailable()) return 0;
  const cutoff = Date.now() - maxAgeMs;
  // Maintenance only: an unreachable store means "not this time", never an
  // error the user has to see. It runs again on the next visit.
  let sessions: StoredRecordingSession[];
  try {
    sessions = await listStoredSessions();
  } catch {
    return 0;
  }
  let purged = 0;
  for (const session of sessions) {
    if (session.updatedAt <= cutoff) {
      try {
        await deleteStoredSession(session.id);
        purged += 1;
      } catch {
        // Leave it for the next sweep rather than aborting the whole pass.
      }
    }
  }
  return purged;
}
