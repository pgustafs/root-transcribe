/** The parts of a notebook entry that the language model or the full
 * recording adds: text on the slide, key terms, wider context, glossary. */

import { useQuery } from "@tanstack/react-query";
import {
  CheckIcon,
  ChevronDownIcon,
  CircleHelpIcon,
  CopyIcon,
  MessageCircleQuestionIcon,
  StarIcon,
} from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import * as studyApi from "@/api/study";
import type { NoteTag, StudyNote } from "@/api/study";
import type { GlossaryEntry } from "@/lib/study/noteText";
import { Markdown } from "@/components/Markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Spinner } from "@/components/ui/spinner";
import { noteClock } from "@/utils/formatDuration";

const TAG_ICON = {
  important: StarIcon,
  unclear: CircleHelpIcon,
  question: MessageCircleQuestionIcon,
} as const;

export function TagBadges({ tags }: { tags: NoteTag[] }) {
  const { t } = useTranslation("study");
  if (tags.length === 0) return null;
  return (
    <span className="flex flex-wrap gap-1.5">
      {tags.map((tag) => {
        const Icon = TAG_ICON[tag];
        return (
          <Badge key={tag} variant="secondary">
            <Icon aria-hidden="true" />
            {t(`marks.${tag}`)}
          </Badge>
        );
      })}
    </span>
  );
}

/** What can be read in the image, folded away until asked for. */
export function SlideText({ text }: { text: string }) {
  const { t } = useTranslation("study");
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Collapsible className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <CollapsibleTrigger asChild>
          <Button variant="ghost" size="sm" className="group -ml-2.5">
            <ChevronDownIcon
              data-icon="inline-start"
              className="transition-transform group-data-[state=closed]:-rotate-90"
            />
            {t("page.slideText")}
          </Button>
        </CollapsibleTrigger>
        <Button variant="ghost" size="sm" onClick={() => void copy()}>
          {copied ? (
            <CheckIcon data-icon="inline-start" />
          ) : (
            <CopyIcon data-icon="inline-start" />
          )}
          <span aria-live="polite">
            {copied ? t("page.copied") : t("page.copy")}
          </span>
        </Button>
      </div>
      <CollapsibleContent>
        <div className="rounded-md border bg-foreground/5 p-3 text-sm">
          <Markdown>{text}</Markdown>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

export function KeyTerms({ note }: { note: StudyNote }) {
  const { t } = useTranslation("study");
  if (!note.terms?.length) return null;
  return (
    <div className="flex flex-col gap-2">
      <h4 className="eyebrow px-0">{t("page.terms")}</h4>
      <dl className="flex flex-col gap-1.5 text-sm">
        {note.terms.map((item) => (
          <div key={item.term}>
            <dt className="inline font-bold">{item.term}: </dt>
            <dd className="inline text-body">{item.definition}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/**
 * "Show 30 seconds more": what was said before and after the note, from the
 * transcript of the full recording. Each press widens the range.
 */
export function WiderContext({
  workspaceId,
  note,
}: {
  workspaceId: string;
  note: StudyNote;
}) {
  const { t } = useTranslation("study");
  const [seconds, setSeconds] = useState(0);
  const context = useQuery({
    queryKey: ["study", workspaceId, "context", note.id, seconds] as const,
    queryFn: () => studyApi.getNoteContext(workspaceId, note.id, seconds),
    enabled: seconds > 0,
    placeholderData: (previous) => previous,
  });
  const data = seconds > 0 ? context.data : undefined;
  const complete = Boolean(data?.available && data.at_start && data.at_end);
  const regionId = `note-context-${note.id}`;
  return (
    <div className="flex flex-col gap-2">
      {data?.available && (
        <div
          id={regionId}
          role="region"
          aria-label={t("page.contextLabel", {
            start: noteClock(data.start_ms),
            end: noteClock(data.end_ms),
          })}
          className="rounded-md border bg-foreground/5 p-3"
        >
          <p className="mb-2 text-xs text-muted-foreground">
            {t("page.contextRange", {
              start: noteClock(data.start_ms),
              end: noteClock(data.end_ms),
            })}
          </p>
          {data.segments.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t("page.contextEmpty")}
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5 text-sm">
              {data.segments.map((segment) => (
                <li key={`${segment.start_ms}-${segment.end_ms}`}>
                  <span className="text-xs text-muted-foreground">
                    {noteClock(segment.start_ms)}
                    {segment.speaker ? ` · ${segment.speaker}` : ""}
                  </span>{" "}
                  <span className="text-body">{segment.text}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2" aria-live="polite">
        {!complete && (
          <Button
            variant="outline"
            size="sm"
            disabled={context.isFetching}
            onClick={() => setSeconds((value) => Math.min(600, value + 30))}
            aria-controls={data?.available ? regionId : undefined}
          >
            {context.isFetching && <Spinner data-icon="inline-start" />}
            {t("page.contextMore")}
          </Button>
        )}
        {seconds > 0 && data?.available && (
          <Button variant="ghost" size="sm" onClick={() => setSeconds(0)}>
            {t("page.contextHide")}
          </Button>
        )}
        {data && !data.available && (
          <span className="text-sm text-muted-foreground">
            {t(`page.contextReason.${data.reason ?? "not_linked"}`)}
          </span>
        )}
        {context.isError && (
          <span className="text-sm text-muted-foreground">
            {t("page.contextFailed")}
          </span>
        )}
      </div>
    </div>
  );
}

export function Glossary({
  entries,
  onOpenNote,
}: {
  entries: GlossaryEntry[];
  onOpenNote: (note: StudyNote) => void;
}) {
  const { t } = useTranslation("study");
  if (entries.length === 0)
    return (
      <p className="text-sm text-muted-foreground">{t("page.glossaryEmpty")}</p>
    );
  return (
    <dl className="glass-card flex flex-col divide-y rounded-xl border">
      {entries.map((entry) => (
        <div key={entry.term} className="flex flex-col gap-1 p-4">
          <dt className="font-heading font-medium">{entry.term}</dt>
          <dd className="text-sm text-body">{entry.definition}</dd>
          <dd className="flex flex-wrap gap-1.5">
            {entry.notes.map((note) => (
              <Button
                key={note.id}
                variant="link"
                size="sm"
                className="h-auto p-0"
                onClick={() => onOpenNote(note)}
              >
                {note.title ??
                  t("page.at", { time: noteClock(note.captured_ms) })}
              </Button>
            ))}
          </dd>
        </div>
      ))}
    </dl>
  );
}
