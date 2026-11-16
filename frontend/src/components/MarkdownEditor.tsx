import { useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  BoldIcon,
  HeadingIcon,
  ItalicIcon,
  LinkIcon,
  ListChecksIcon,
  ListIcon,
} from "lucide-react";

import { Markdown } from "@/components/Markdown";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type Edit = { text: string; start: number; end: number };

/** Wrap the selection, e.g. **bold**; with no selection, insert a placeholder. */
function wrap(
  value: string,
  start: number,
  end: number,
  mark: string,
  placeholder: string,
  suffix = mark,
): Edit {
  const selected = value.slice(start, end) || placeholder;
  const text =
    value.slice(0, start) + mark + selected + suffix + value.slice(end);
  return {
    text,
    start: start + mark.length,
    end: start + mark.length + selected.length,
  };
}

/** Put a prefix at the start of every selected line (lists, headings). */
function prefixLines(
  value: string,
  start: number,
  end: number,
  prefix: string,
): Edit {
  const from = value.lastIndexOf("\n", start - 1) + 1;
  const nextBreak = value.indexOf("\n", end);
  const to = nextBreak === -1 ? value.length : nextBreak;
  const block = value
    .slice(from, to)
    .split("\n")
    .map((line) => (line.startsWith(prefix) ? line : prefix + line))
    .join("\n");
  return {
    text: value.slice(0, from) + block + value.slice(to),
    start: from,
    end: from + block.length,
  };
}

/**
 * Markdown editor: a Write tab (plain text with a small formatting toolbar)
 * and a Preview tab (how it will look). The value is always the Markdown
 * source, so what is saved is exactly what was typed.
 */
export function MarkdownEditor({
  value,
  onChange,
  label,
  placeholder,
  rows = 16,
  showPreview = true,
  invalid = false,
  describedBy,
  id,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  /** Accessible name of the text area. */
  label: string;
  placeholder?: string;
  rows?: number;
  /** Instructions for an agent are plain text: no preview needed. */
  showPreview?: boolean;
  invalid?: boolean;
  describedBy?: string;
  id?: string;
  className?: string;
}) {
  const { t } = useTranslation("notes");
  const generatedId = useId();
  const areaId = id ?? generatedId;
  const area = useRef<HTMLTextAreaElement>(null);
  const [tab, setTab] = useState("write");

  const apply = (edit: (v: string, s: number, e: number) => Edit) => {
    const el = area.current;
    if (!el) return;
    const next = edit(value, el.selectionStart, el.selectionEnd);
    onChange(next.text);
    // Restore the selection after React has written the new value.
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(next.start, next.end);
    });
  };

  const tools = [
    {
      key: "heading",
      icon: HeadingIcon,
      run: (v: string, s: number, e: number) => prefixLines(v, s, e, "## "),
    },
    {
      key: "bold",
      icon: BoldIcon,
      run: (v: string, s: number, e: number) =>
        wrap(v, s, e, "**", t("editor.placeholderText")),
    },
    {
      key: "italic",
      icon: ItalicIcon,
      run: (v: string, s: number, e: number) =>
        wrap(v, s, e, "*", t("editor.placeholderText")),
    },
    {
      key: "list",
      icon: ListIcon,
      run: (v: string, s: number, e: number) => prefixLines(v, s, e, "- "),
    },
    {
      key: "checklist",
      icon: ListChecksIcon,
      run: (v: string, s: number, e: number) => prefixLines(v, s, e, "- [ ] "),
    },
    {
      key: "link",
      icon: LinkIcon,
      run: (v: string, s: number, e: number) =>
        wrap(v, s, e, "[", t("editor.placeholderText"), "](https://)"),
    },
  ] as const;

  const textarea = (
    <Textarea
      ref={area}
      id={areaId}
      value={value}
      rows={rows}
      aria-label={label}
      aria-invalid={invalid || undefined}
      aria-describedby={describedBy}
      placeholder={placeholder}
      spellCheck={true}
      className="min-h-40 resize-y font-mono text-sm leading-relaxed"
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );

  if (!showPreview) return <div className={className}>{textarea}</div>;

  return (
    <Tabs value={tab} onValueChange={setTab} className={cn("gap-2", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <TabsList aria-label={t("editor.modes")}>
          <TabsTrigger value="write">{t("editor.write")}</TabsTrigger>
          <TabsTrigger value="preview">{t("editor.preview")}</TabsTrigger>
        </TabsList>
        {tab === "write" && (
          <div
            role="toolbar"
            aria-label={t("editor.toolbar")}
            className="flex items-center gap-0.5"
          >
            {tools.map(({ key, icon: Icon, run }) => (
              <Tooltip key={key}>
                <TooltipTrigger
                  render={
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={t(`editor.tools.${key}`)}
                      className="text-muted-foreground"
                      onClick={() => apply(run)}
                    />
                  }
                >
                  <Icon />
                </TooltipTrigger>
                <TooltipContent>{t(`editor.tools.${key}`)}</TooltipContent>
              </Tooltip>
            ))}
          </div>
        )}
      </div>
      <TabsContent value="write">{textarea}</TabsContent>
      <TabsContent value="preview">
        <div className="min-h-40 rounded-md border px-4 py-3">
          {value.trim() ? (
            <Markdown>{value}</Markdown>
          ) : (
            <p className="text-sm text-muted-foreground">
              {t("editor.emptyPreview")}
            </p>
          )}
        </div>
      </TabsContent>
    </Tabs>
  );
}
