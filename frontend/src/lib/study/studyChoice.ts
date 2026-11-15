/** What the learner chose in "Take study notes": which notebook, which mode. */

import * as studyApi from "@/api/study";
import type { StudyNotebook } from "@/api/study";
import type { StudyMode } from "@/lib/study/noteStore";
import { isHttpUrl } from "@/lib/study/sourceKey";

export type StudyChoice = {
  /** "" = a new notebook. */
  notebookId: string;
  sourceUrl: string;
  title: string;
  mode: StudyMode;
  /** Let the language model work on the notes (when one is configured). */
  enrich: boolean;
  /** Offer to keep a slide when it changes. */
  offerSlides: boolean;
};

export const EMPTY_STUDY_CHOICE: StudyChoice = {
  notebookId: "",
  sourceUrl: "",
  title: "",
  mode: "until_stopped",
  enrich: true,
  offerSlides: true,
};

export function sourceUrlProblem(choice: StudyChoice): boolean {
  return (
    !choice.notebookId &&
    choice.sourceUrl.trim() !== "" &&
    !isHttpUrl(choice.sourceUrl)
  );
}

/** Open the chosen notebook: the picked one, the one this source already
 * has, or a new one. */
export async function resolveNotebook(
  workspaceId: string,
  choice: StudyChoice,
  notebooks: StudyNotebook[],
  fallbackTitle: string,
): Promise<StudyNotebook> {
  const picked = notebooks.find((n) => n.id === choice.notebookId);
  if (picked) return picked;
  return studyApi.createNotebook(workspaceId, {
    title: choice.title.trim() || fallbackTitle,
    source_url: choice.sourceUrl.trim() || null,
  });
}
