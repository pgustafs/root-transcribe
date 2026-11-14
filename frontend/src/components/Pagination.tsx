import { useTranslation } from "react-i18next";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronsLeftIcon,
  ChevronsRightIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
type Props = {
  itemCount?: number;
  page?: number;
  perPage?: number;
  onSetPage?: (event: unknown, page: number) => void;
  onPerPageSelect?: (event: unknown, perPage: number) => void;
  isCompact?: boolean;
};
export function Pagination({
  itemCount = 0,
  page = 1,
  perPage = 10,
  onSetPage,
  onPerPageSelect,
}: Props) {
  const { t } = useTranslation("common");
  const total = Math.max(1, Math.ceil(itemCount / perPage));
  return (
    <nav
      aria-label={t("pagination.label")}
      className="flex flex-wrap items-center gap-2 text-sm"
    >
      <span className="text-muted-foreground">
        {itemCount ? Math.min((page - 1) * perPage + 1, itemCount) : 0}–
        {Math.min(page * perPage, itemCount)} {t("pagination.of")} {itemCount}
      </span>
      <NativeSelect
        aria-label={t("pagination.itemsPerPage")}
        value={perPage}
        onChange={(e) => onPerPageSelect?.(e, Number(e.target.value))}
      >
        {[10, 20, 50, 100].map((n) => (
          <NativeSelectOption key={n} value={n}>
            {n}
          </NativeSelectOption>
        ))}
      </NativeSelect>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t("pagination.toFirstPage")}
        disabled={page <= 1}
        onClick={(e) => onSetPage?.(e, 1)}
      >
        <ChevronsLeftIcon data-icon="inline-start" />
      </Button>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t("pagination.toPreviousPage")}
        disabled={page <= 1}
        onClick={(e) => onSetPage?.(e, page - 1)}
      >
        <ChevronLeftIcon data-icon="inline-start" />
      </Button>
      <span>
        {page} / {total}
      </span>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t("pagination.toNextPage")}
        disabled={page >= total}
        onClick={(e) => onSetPage?.(e, page + 1)}
      >
        <ChevronRightIcon data-icon="inline-start" />
      </Button>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t("pagination.toLastPage")}
        disabled={page >= total}
        onClick={(e) => onSetPage?.(e, total)}
      >
        <ChevronsRightIcon data-icon="inline-start" />
      </Button>
    </nav>
  );
}
