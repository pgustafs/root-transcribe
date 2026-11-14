import type { ChangeEvent } from "react";
import { useTranslation } from "react-i18next";
import { SearchIcon, XIcon } from "lucide-react";
import {
  InputGroup,
  InputGroupInput,
  InputGroupAddon,
  InputGroupButton,
} from "@/components/ui/input-group";
type Props = {
  value?: string;
  onChange?: (event: ChangeEvent<HTMLInputElement>, value: string) => void;
  onClear?: () => void;
  placeholder?: string;
  "aria-label": string;
  className?: string;
  isDisabled?: boolean;
  resultsCount?: number;
};
export function SearchInput({
  value,
  onChange,
  onClear,
  isDisabled,
  resultsCount,
  ...props
}: Props) {
  const { t } = useTranslation("common");
  return (
    <InputGroup className={props.className}>
      <InputGroupAddon>
        <SearchIcon />
      </InputGroupAddon>
      <InputGroupInput
        {...props}
        className={undefined}
        value={value}
        disabled={isDisabled}
        onChange={(event) => onChange?.(event, event.target.value)}
      />
      {resultsCount !== undefined && (
        <InputGroupAddon align="inline-end">
          <span aria-live="polite">{resultsCount}</span>
        </InputGroupAddon>
      )}
      {value && onClear && (
        <InputGroupAddon align="inline-end">
          <InputGroupButton
            size="icon-xs"
            aria-label={t("search.reset")}
            onClick={onClear}
          >
            <XIcon />
          </InputGroupButton>
        </InputGroupAddon>
      )}
    </InputGroup>
  );
}
