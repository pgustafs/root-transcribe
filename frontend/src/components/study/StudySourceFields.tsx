/** Where study notes go: an existing notebook, or a new one for a source. */

import { useTranslation } from "react-i18next";

import type { StudyNotebook } from "@/api/study";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { StudyMode } from "@/lib/study/noteStore";
import { sourceUrlProblem, type StudyChoice } from "@/lib/study/studyChoice";
import { sourceKey } from "@/lib/study/sourceKey";

type Props = {
  idPrefix: string;
  choice: StudyChoice;
  onChange: (choice: StudyChoice) => void;
  notebooks: StudyNotebook[];
  /** Suggested notebook title (the recording title). */
  suggestedTitle: string;
  showMode?: boolean;
  /** Ask for the source's link (a video); not for a meeting in a room. */
  showSource?: boolean;
};

export function StudySourceFields({
  idPrefix,
  choice,
  onChange,
  notebooks,
  suggestedTitle,
  showMode = true,
  showSource = true,
}: Props) {
  const { t } = useTranslation("study");
  const key = sourceKey(choice.sourceUrl);
  const existing = key
    ? notebooks.find((n) => n.source_key === key)
    : undefined;
  const invalid = sourceUrlProblem(choice);
  const isNew = !choice.notebookId;

  return (
    <>
      {notebooks.length > 0 && (
        <Field>
          <FieldLabel htmlFor={`${idPrefix}-notebook`}>
            {t("setup.notebook")}
          </FieldLabel>
          <NativeSelect
            id={`${idPrefix}-notebook`}
            value={choice.notebookId}
            onChange={(event) =>
              onChange({ ...choice, notebookId: event.currentTarget.value })
            }
          >
            <NativeSelectOption value="">
              {t("setup.notebookNew")}
            </NativeSelectOption>
            {notebooks.map((notebook) => (
              <NativeSelectOption key={notebook.id} value={notebook.id}>
                {notebook.title}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
      )}
      {isNew && (
        <>
          <Field
            data-invalid={invalid || undefined}
            className={showSource ? undefined : "hidden"}
          >
            <FieldLabel htmlFor={`${idPrefix}-source`}>
              {t("setup.sourceUrl")}
            </FieldLabel>
            <Input
              id={`${idPrefix}-source`}
              type="url"
              inputMode="url"
              value={choice.sourceUrl}
              placeholder={t("setup.sourceUrlPlaceholder")}
              aria-invalid={invalid || undefined}
              aria-describedby={`${idPrefix}-source-help`}
              onChange={(event) =>
                onChange({ ...choice, sourceUrl: event.currentTarget.value })
              }
            />
            <FieldDescription id={`${idPrefix}-source-help`}>
              {invalid
                ? t("setup.sourceUrlInvalid")
                : existing
                  ? t("setup.sourceExists", { title: existing.title })
                  : t("setup.sourceUrlHelp")}
            </FieldDescription>
          </Field>
          {!existing && (
            <Field>
              <FieldLabel htmlFor={`${idPrefix}-title`}>
                {t("setup.notebookTitle")}
              </FieldLabel>
              <Input
                id={`${idPrefix}-title`}
                value={choice.title}
                placeholder={suggestedTitle}
                maxLength={255}
                onChange={(event) =>
                  onChange({ ...choice, title: event.currentTarget.value })
                }
              />
            </Field>
          )}
        </>
      )}
      {showMode && (
        <Field>
          <FieldLabel id={`${idPrefix}-mode-label`}>
            {t("setup.mode")}
          </FieldLabel>
          <ToggleGroup
            variant="outline"
            aria-labelledby={`${idPrefix}-mode-label`}
            className="flex-wrap justify-start"
            value={[choice.mode]}
            onValueChange={([value]) => {
              if (value) onChange({ ...choice, mode: value as StudyMode });
            }}
          >
            <ToggleGroupItem value="until_stopped">
              {t("setup.modeUntilStopped")}
            </ToggleGroupItem>
            <ToggleGroupItem value="last_15s">
              {t("setup.modeLast15")}
            </ToggleGroupItem>
          </ToggleGroup>
          <FieldDescription>
            {choice.mode === "until_stopped"
              ? t("setup.modeUntilStoppedHelp")
              : t("setup.modeLast15Help")}
          </FieldDescription>
        </Field>
      )}
    </>
  );
}
