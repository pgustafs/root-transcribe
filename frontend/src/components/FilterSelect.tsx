import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectGroup,
  SelectItem,
} from "@/components/ui/select";
export type FilterOption = { value: string; label: string };
type Props = {
  "aria-label": string;
  value: string;
  options: FilterOption[];
  onChange: (value: string) => void;
  isDisabled?: boolean;
};
// Radix reserves the empty string for clearing the selection.
const ALL = "__all__";
export function FilterSelect({
  "aria-label": label,
  value,
  options,
  onChange,
  isDisabled,
}: Props) {
  return (
    <Select
      value={value || ALL}
      onValueChange={(v) => onChange(v === ALL ? "" : v)}
      disabled={isDisabled}
    >
      <SelectTrigger aria-label={label} className="min-w-36">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value || ALL}>
              {o.label}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}
