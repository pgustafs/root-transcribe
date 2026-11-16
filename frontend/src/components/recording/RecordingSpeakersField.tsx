import { useId } from "react";
import { useTranslation } from "react-i18next";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";

/** Mirrors the backend's MIN_SPEAKERS / MAX_SPEAKERS. */
const COUNTS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

/** How many people speak in a recording. `null` lets speaker detection
 * decide, which can invent a speaker out of quiet or overlapping speech. */
export function RecordingSpeakersField({
  value,
  onChange,
}: {
  value: number | null;
  onChange: (value: number | null) => void;
}) {
  const { t } = useTranslation("meetings");
  const id = useId();
  return (
    <Field>
      <FieldLabel htmlFor={id}>{t("recordingSpeakers.label")}</FieldLabel>
      <NativeSelect
        id={id}
        value={value === null ? "" : String(value)}
        aria-describedby={`${id}-help`}
        onChange={(event) => {
          const next = event.currentTarget.value;
          onChange(next === "" ? null : Number(next));
        }}
      >
        <NativeSelectOption value="">
          {t("recordingSpeakers.auto")}
        </NativeSelectOption>
        {COUNTS.map((n) => (
          <NativeSelectOption key={n} value={String(n)}>
            {t("recordingSpeakers.count", { count: n })}
          </NativeSelectOption>
        ))}
      </NativeSelect>
      <FieldDescription id={`${id}-help`}>
        {t("recordingSpeakers.help")}
      </FieldDescription>
    </Field>
  );
}
