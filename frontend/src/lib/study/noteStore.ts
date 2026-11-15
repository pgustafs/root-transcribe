/**
 * Study notes that have not reached the server yet, kept in IndexedDB.
 *
 * Its own database ("study-notes"), so the recorder's recovery database needs
 * no upgrade. Same conventions as `recordingStore.ts`: the record is written
 * before anything is sent, uploads repeat safely because ids are made here,
 * and a reload finds what was left behind.
 *
 * - `sessions`: one per study session (notebook, mode, crop, next sequence).
 * - `notes`: a capture. `open` while audio is still being added, `queued` once
 *   complete, `uploaded` after the server confirmed it (blobs dropped then).
 * - `audio`: an open note's samples in order, written as they arrive.
 */

import { type DBSchema, type IDBPDatabase, openDB } from "idb";

import type { Crop } from "@/lib/study/frame";

export type StudyMode = "until_stopped" | "last_15s";

export type StoredStudySession = {
  id: string;
  workspaceId: string;
  notebookId: string;
  notebookTitle: string;
  language: string;
  mode: StudyMode;
  crop: Crop;
  nextSeq: number;
  startedAt: string;
  /** Let the language model work on the notes (absent = yes). */
  enrich?: boolean;
  /** Offer to keep a slide when it changes (absent = no). */
  offerSlides?: boolean;
  /** The recorder's local session this belongs to (linked at upload). */
  localRecordingId: string;
  /** Set when the meeting recording exists on the server. */
  recordingId: string | null;
  /** The server knows this session's link to its recording. */
  linked: boolean;
  /** The recording is over: nothing more will be captured. */
  ended: boolean;
};

export type StoredNoteState = "open" | "queued" | "uploaded";

export type StoredNote = {
  id: string;
  sessionId: string;
  workspaceId: string;
  notebookId: string;
  seq: number;
  capturedMs: number;
  audioStartMs: number | null;
  audioEndMs: number | null;
  sampleRate: number;
  image: Blob | null;
  audio: Blob | null;
  hasAudio: boolean;
  state: StoredNoteState;
  endReason: string | null;
  /** Marks set at the press (absent in older records). */
  tags?: string[];
  /** A thought written at the press. */
  comment?: string | null;
  createdAt: number;
};

type StoredAudio = { noteId: string; index: number; samples: ArrayBuffer };

interface StudyDB extends DBSchema {
  sessions: { key: string; value: StoredStudySession };
  notes: {
    key: string;
    value: StoredNote;
    indexes: { bySession: string };
  };
  audio: {
    key: [string, number];
    value: StoredAudio;
    indexes: { byNote: string };
  };
}

const DB_NAME = "study-notes";

let dbPromise: Promise<IDBPDatabase<StudyDB>> | null = null;

export function isStudyStoreAvailable(): boolean {
  return typeof indexedDB !== "undefined";
}

function db(): Promise<IDBPDatabase<StudyDB>> {
  if (!dbPromise) {
    dbPromise = openDB<StudyDB>(DB_NAME, 2, {
      upgrade(database, oldVersion) {
        if (oldVersion < 1) {
          database.createObjectStore("sessions", { keyPath: "id" });
          const notes = database.createObjectStore("notes", { keyPath: "id" });
          notes.createIndex("bySession", "sessionId");
          const audio = database.createObjectStore("audio", {
            keyPath: ["noteId", "index"],
          });
          audio.createIndex("byNote", "noteId");
        }
        // Version 1 kept answers to keyboard-shortcut presses; that is gone.
        const names = database.objectStoreNames as unknown as DOMStringList;
        if (names.contains("invocations"))
          (database as unknown as IDBDatabase).deleteObjectStore("invocations");
      },
      blocking(_current, _blocked, event) {
        (event.target as IDBDatabase | null)?.close();
        dbPromise = null;
      },
      terminated() {
        dbPromise = null;
      },
    });
    dbPromise.catch(() => {
      dbPromise = null;
    });
  }
  return dbPromise;
}

/** Test helper: forget the cached connection. */
export async function closeStudyStore(): Promise<void> {
  const open = dbPromise;
  dbPromise = null;
  if (open) (await open.catch(() => null))?.close();
}

// --- sessions -----------------------------------------------------------------

export async function putSession(session: StoredStudySession): Promise<void> {
  await (await db()).put("sessions", session);
}

export async function getSession(
  id: string,
): Promise<StoredStudySession | undefined> {
  return (await db()).get("sessions", id);
}

export async function listSessions(): Promise<StoredStudySession[]> {
  return (await db()).getAll("sessions");
}

export async function updateSession(
  id: string,
  patch: Partial<StoredStudySession>,
): Promise<StoredStudySession | undefined> {
  const database = await db();
  const tx = database.transaction("sessions", "readwrite");
  const current = await tx.store.get(id);
  if (!current) {
    await tx.done;
    return undefined;
  }
  const next = { ...current, ...patch };
  await tx.store.put(next);
  await tx.done;
  return next;
}

/** Take the next capture number of a session (0, 1, 2 …), durably. */
export async function takeSeq(sessionId: string): Promise<number> {
  const database = await db();
  const tx = database.transaction("sessions", "readwrite");
  const current = await tx.store.get(sessionId);
  if (!current) {
    await tx.done;
    throw new Error("The study session is not stored");
  }
  await tx.store.put({ ...current, nextSeq: current.nextSeq + 1 });
  await tx.done;
  return current.nextSeq;
}

/** Remove a session with every note and sample that belongs to it. */
export async function deleteSession(id: string): Promise<void> {
  const database = await db();
  const tx = database.transaction(["sessions", "notes", "audio"], "readwrite");
  const notes = await tx.objectStore("notes").index("bySession").getAllKeys(id);
  for (const noteId of notes) {
    const audio = tx.objectStore("audio");
    for (const key of await audio.index("byNote").getAllKeys(noteId))
      await audio.delete(key);
    await tx.objectStore("notes").delete(noteId);
  }
  await tx.objectStore("sessions").delete(id);
  await tx.done;
}

// --- notes --------------------------------------------------------------------

export async function putNote(note: StoredNote): Promise<void> {
  await (await db()).put("notes", note);
}

/** Change fields of a stored note; nothing happens when it is gone. */
export async function patchNote(
  id: string,
  patch: Partial<StoredNote>,
): Promise<void> {
  const database = await db();
  const tx = database.transaction("notes", "readwrite");
  const current = await tx.store.get(id);
  if (current) await tx.store.put({ ...current, ...patch });
  await tx.done;
}

export async function getNote(id: string): Promise<StoredNote | undefined> {
  return (await db()).get("notes", id);
}

export async function listNotes(sessionId: string): Promise<StoredNote[]> {
  const notes = await (
    await db()
  ).getAllFromIndex("notes", "bySession", sessionId);
  return notes.sort((a, b) => a.seq - b.seq);
}

export async function listNotesInState(
  state: StoredNoteState,
): Promise<StoredNote[]> {
  const notes = await (await db()).getAll("notes");
  return notes
    .filter((note) => note.state === state)
    .sort((a, b) => a.createdAt - b.createdAt);
}

/** The server has it: keep the record (for counts), drop the media. */
export async function markUploaded(id: string): Promise<void> {
  const database = await db();
  const tx = database.transaction(["notes", "audio"], "readwrite");
  const note = await tx.objectStore("notes").get(id);
  if (note) {
    await tx
      .objectStore("notes")
      .put({ ...note, state: "uploaded", image: null, audio: null });
  }
  const audio = tx.objectStore("audio");
  for (const key of await audio.index("byNote").getAllKeys(id))
    await audio.delete(key);
  await tx.done;
}

// --- an open note's audio -------------------------------------------------------

export async function appendAudio(
  noteId: string,
  index: number,
  samples: Float32Array,
): Promise<void> {
  // A copy with its own buffer: the caller's array may be a view.
  const copy = samples.slice().buffer;
  await (await db()).put("audio", { noteId, index, samples: copy });
}

/** An open note's samples, in order. */
export async function readAudio(noteId: string): Promise<Float32Array[]> {
  const rows = await (await db()).getAllFromIndex("audio", "byNote", noteId);
  return rows
    .sort((a, b) => a.index - b.index)
    .map((row) => new Float32Array(row.samples));
}
