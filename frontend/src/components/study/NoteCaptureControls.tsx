import { CircleHelpIcon, StarIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { NoteTag } from "@/api/study";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { StudyStatus } from "@/lib/study/noteController";
import { cn } from "@/lib/utils";

/** What the capture controls can ask for. A thought typed in the field goes
 * along with whichever button is pressed next. */
export type NoteCaptureActions = {
  onToggle: (thought?: string) => void;
  onMark: (tag: NoteTag, thought?: string) => void;
  onThought: (thought: string) => void;
  onKeepOffer: (thought?: string) => void;
};

type Props = {
  status: StudyStatus;
  /** A new note can be taken now (an open one can always be stopped). */
  canCapture: boolean;
  actions: NoteCaptureActions;
  /** "floating": stacked, for the small window. "dock": one wrapping row. */
  variant: "floating" | "dock";
  /** Id of the text that says what is going on. */
  describedBy?: string;
  focusOnMount?: boolean;
};

const MARKS: { tag: NoteTag; icon: typeof StarIcon }[] = [
  { tag: "important", icon: StarIcon },
  { tag: "unclear", icon: CircleHelpIcon },
];

/**
 * Take note / Stop note, the two marks, a one-line thought and (when a slide
 * just changed) the offer to keep the previous slide.
 */
export function NoteCaptureControls({
  status,
  canCapture,
  actions,
  variant,
  describedBy,
  focusOnMount = false,
}: Props) {
  const { t } = useTranslation("study");
  const [thought, setThought] = useState("");
  const main = useRef<HTMLButtonElement>(null);
  const noteOpen = status.openSinceMs !== null;
  const usable = noteOpen || canCapture;
  const floating = variant === "floating";

  useEffect(() => {
    if (focusOnMount) main.current?.focus();
  }, [focusOnMount]);

  /** Hand the typed thought to an action and empty the field. */
  const withThought = (action: (thought?: string) => void) => {
    const text = thought.trim();
    setThought("");
    action(text || undefined);
  };

  return (
    <div
      className={cn(
        "flex gap-2",
        floating ? "flex-col" : "flex-wrap items-center",
      )}
    >
      <Button
        ref={main}
        onClick={() => withThought(actions.onToggle)}
        disabled={!usable}
        variant="default"
        size={floating ? "lg" : "sm"}
        className={floating ? "w-full" : undefined}
        aria-describedby={describedBy}
      >
        {noteOpen ? t("dock.stopNote") : t("dock.takeNote")}
      </Button>
      <div
        role="group"
        aria-label={t("marks.label")}
        className={cn("flex flex-wrap gap-2", floating && "*:flex-1")}
      >
        {MARKS.map(({ tag, icon: Icon }) => {
          const on = noteOpen && status.openTags.includes(tag);
          return (
            <Button
              key={tag}
              onClick={() => withThought((text) => actions.onMark(tag, text))}
              disabled={!usable}
              variant="outline"
              size="sm"
              // Pressed only shows on an open note; otherwise the button
              // takes a note that carries the mark.
              aria-pressed={noteOpen ? on : undefined}
              className={on ? "bg-foreground/8" : undefined}
            >
              <Icon data-icon="inline-start" />
              {t(`marks.${tag}`)}
            </Button>
          );
        })}
      </div>
      <Input
        value={thought}
        disabled={!usable}
        maxLength={500}
        aria-label={t("thought.label")}
        placeholder={t("thought.placeholder")}
        className={floating ? undefined : "w-56"}
        onChange={(event) => setThought(event.currentTarget.value)}
        onKeyDown={(event) => {
          if (event.key !== "Enter" || !thought.trim()) return;
          event.preventDefault();
          actions.onThought(thought.trim());
          setThought("");
        }}
      />
      {status.offer && (
        <Button
          onClick={() => withThought(actions.onKeepOffer)}
          variant="outline"
          size="sm"
          className={floating ? "w-full" : undefined}
        >
          {t("offer.keep")}
        </Button>
      )}
      <span className="sr-only" aria-live="polite">
        {status.offer ? t("offer.announce") : ""}
      </span>
    </div>
  );
}
