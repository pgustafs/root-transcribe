import { useId } from "react";
import { useTranslation } from "react-i18next";
import type { RecordingLanguage } from "@/api/types";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export function RecordingLanguageField({
  value,
  onChange,
}: {
  value: RecordingLanguage;
  onChange: (value: RecordingLanguage) => void;
}) {
  const { t } = useTranslation("meetings");
  const id = useId();
  return (
    <Field>
      <FieldLabel id={`${id}-label`}>{t("recordingLanguage.label")}</FieldLabel>
      <ToggleGroup
        variant="outline"
        value={[value]}
        aria-labelledby={`${id}-label`}
        aria-describedby={`${id}-help`}
        onValueChange={([next]) => {
          if (next === "auto" || next === "en" || next === "sv") onChange(next);
        }}
      >
        <ToggleGroupItem value="auto">
          {t("recordingLanguage.auto")}
        </ToggleGroupItem>
        <ToggleGroupItem value="en">
          {t("recordingLanguage.en")}
        </ToggleGroupItem>
        <ToggleGroupItem value="sv">
          {t("recordingLanguage.sv")}
        </ToggleGroupItem>
      </ToggleGroup>
      <FieldDescription id={`${id}-help`}>
        {t("recordingLanguage.help")}
      </FieldDescription>
    </Field>
  );
}
