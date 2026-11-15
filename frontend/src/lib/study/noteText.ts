/** Searching notes and collecting their key terms. */

import type { StudyNote } from "@/api/study";

export type GlossaryEntry = {
  term: string;
  definition: string;
  /** Notes that use the term, in capture order. */
  notes: StudyNote[];
};

/** Every key term of a notebook once, A to Z. */
export function buildGlossary(
  notes: StudyNote[],
  locale: string,
): GlossaryEntry[] {
  const entries = new Map<string, GlossaryEntry>();
  for (const note of notes) {
    for (const item of note.terms ?? []) {
      const key = item.term.toLocaleLowerCase(locale);
      const entry = entries.get(key);
      if (entry) entry.notes.push(note);
      else entries.set(key, { ...item, notes: [note] });
    }
  }
  return [...entries.values()].sort((a, b) =>
    a.term.localeCompare(b.term, locale, { sensitivity: "base" }),
  );
}

/** Does a note contain the words that were searched for? */
export function noteMatches(note: StudyNote, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = [
    note.title,
    note.text,
    note.clean_text,
    note.slide_text,
    note.comment,
    note.explanation,
    ...(note.terms ?? []).flatMap((item) => [item.term, item.definition]),
  ]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
  return words.every((word) => haystack.includes(word));
}
