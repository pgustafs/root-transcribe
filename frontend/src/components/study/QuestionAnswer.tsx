/** A question that was marked in a meeting, and the answer suggested for it. */

import { ExternalLinkIcon, XIcon } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link as RouterLink } from "react-router-dom";

import type { AnswerSource, StudyNote } from "@/api/study";
import { Markdown } from "@/components/Markdown";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { answerWorking } from "@/hooks/useStudy";
import { noteClock } from "@/utils/formatDuration";

function SourceLink({
  workspaceId,
  source,
}: {
  workspaceId: string;
  source: AnswerSource;
}) {
  const { t } = useTranslation("study");
  const className = "text-link underline underline-offset-4";
  if (source.kind === "web")
    return (
      <a
        href={source.url}
        target="_blank"
        rel="noreferrer noopener"
        className={`${className} inline-flex items-center gap-1 break-all`}
      >
        {source.title}
        <ExternalLinkIcon className="size-3.5 shrink-0" aria-hidden="true" />
      </a>
    );
  if (source.kind === "recording")
    return (
      <RouterLink
        className={className}
        to={`/workspaces/${workspaceId}?recording=${source.recording_id}&t=${Math.floor(source.start_ms / 1000)}`}
      >
        {t("answer.sourceRecording", {
          label: source.label,
          title: source.title,
          time: noteClock(source.start_ms),
        })}
      </RouterLink>
    );
  return (
    <RouterLink
      className={className}
      to={`/workspaces/${workspaceId}/study/${source.notebook_id}`}
    >
      {t("answer.sourceNote", { label: source.label, title: source.title })}
    </RouterLink>
  );
}

/** Where an answer comes from: the workspace first, then the web. */
export function AnswerSources({
  workspaceId,
  sources,
}: {
  workspaceId: string;
  sources: AnswerSource[];
}) {
  const { t } = useTranslation("study");
  if (sources.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      <h5 className="text-xs font-bold text-muted-foreground">
        {t("answer.sources")}
      </h5>
      <ul className="flex flex-col gap-1 text-sm">
        {sources.map((source, index) => (
          <li key={`${source.kind}-${index}`}>
            <SourceLink workspaceId={workspaceId} source={source} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** In words: what is happening with the answer, or why there is none. */
function useAnswerState(note: StudyNote | undefined): string | null {
  const { t } = useTranslation("study");
  if (!note || note.status === "pending" || note.status === "processing")
    return t("answer.listening");
  if (note.answer_status === "failed") {
    if (note.answer_error === "no_question") return t("answer.noQuestion");
    if (note.answer_error === "no_speech") return t("answer.noSpeech");
    return t("answer.failed");
  }
  if (note.answer_status === "pending") return t("answer.finding");
  if (note.answer_status === "processing")
    return note.question ? t("answer.researching") : t("answer.finding");
  if (note.answer_status === "none") return t("answer.unavailable");
  return null;
}

type CardProps = {
  workspaceId: string;
  /** Undefined until the note has reached the server. */
  note: StudyNote | undefined;
  onDismiss: () => void;
  /** The small window: the short answer only. */
  compact?: boolean;
};

/**
 * The answer card shown while the meeting goes on: the question as it was
 * understood, a short answer to say aloud, and where the rest can be read.
 */
export function QuestionAnswerCard({
  workspaceId,
  note,
  onDismiss,
  compact = false,
}: CardProps) {
  const { t } = useTranslation(["study", "common"]);
  const state = useAnswerState(note);
  const working = answerWorking(note);
  return (
    <section
      aria-label={t("study:answer.title")}
      className="flex flex-col gap-2 rounded-md border bg-foreground/5 p-3 text-sm"
      data-testid="question-answer"
    >
      <div className="flex items-start justify-between gap-2">
        <h4 className="eyebrow px-0">{t("study:answer.title")}</h4>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={t("study:answer.dismiss")}
          onClick={onDismiss}
        >
          <XIcon />
        </Button>
      </div>
      <div aria-live="polite" className="flex flex-col gap-2">
        {note?.question && (
          <p className="font-bold text-foreground">{note.question}</p>
        )}
        {note?.answer_short && (
          <p className="app-copy whitespace-pre-wrap text-body">
            {note.answer_short}
          </p>
        )}
        {state && (
          <p className="flex items-center gap-2 text-muted-foreground">
            {working && <Spinner aria-hidden="true" />}
            {state}
          </p>
        )}
      </div>
      {note?.answer_status === "ready" && (
        <>
          {!compact && note.answer_sources && (
            <AnswerSources
              workspaceId={workspaceId}
              sources={note.answer_sources}
            />
          )}
          <p className="text-xs text-muted-foreground">
            {compact
              ? t("study:answer.moreInNotebook")
              : t("study:answer.check")}
          </p>
        </>
      )}
    </section>
  );
}

/** On the notebook page: the question, the whole answer and its sources. */
export function QuestionBlock({
  workspaceId,
  note,
  onRetry,
  canRetry,
}: {
  workspaceId: string;
  note: StudyNote;
  onRetry: () => void;
  canRetry: boolean;
}) {
  const { t } = useTranslation("study");
  const state = useAnswerState(note);
  const working = answerWorking(note);
  const answer = note.answer ?? note.answer_short;
  return (
    <div className="flex flex-col gap-2">
      <h4 className="eyebrow px-0">{t("answer.question")}</h4>
      {note.question && (
        <p className="font-bold text-foreground">{note.question}</p>
      )}
      {answer && (
        <>
          <h4 className="eyebrow px-0">{t("answer.suggested")}</h4>
          <div className="text-sm">
            <Markdown>{answer}</Markdown>
          </div>
        </>
      )}
      {note.answer_status === "ready" && note.answer_sources && (
        <AnswerSources
          workspaceId={workspaceId}
          sources={note.answer_sources}
        />
      )}
      {state && (
        <p
          className="flex flex-wrap items-center gap-2 text-sm"
          role={working ? "status" : undefined}
        >
          {working && <Spinner aria-hidden="true" />}
          {state}
          {!working && canRetry && note.answer_error !== "no_speech" && (
            <Button variant="outline" size="sm" onClick={onRetry}>
              {t("answer.again")}
            </Button>
          )}
        </p>
      )}
      {answer && (
        <p className="text-xs text-muted-foreground">{t("answer.check")}</p>
      )}
    </div>
  );
}
