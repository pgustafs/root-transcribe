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
// The empty string means "no filter"; the list uses a sentinel for it.
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
      // Base UI's SelectValue shows the raw value unless it knows the labels.
      items={options.map((o) => ({ value: o.value || ALL, label: o.label }))}
      value={value || ALL}
      onValueChange={(v) => onChange(!v || v === ALL ? "" : v)}
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
