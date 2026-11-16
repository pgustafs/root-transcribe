import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { MoreVerticalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
type Action = {
  title?: ReactNode;
  onClick?: () => void;
  isDanger?: boolean;
  isDisabled?: boolean;
  isSeparator?: boolean;
};
export function RowActions({
  toggleAriaLabel,
  items,
}: {
  toggleAriaLabel?: string;
  items: Action[];
}) {
  const { t } = useTranslation("common");
  if (!items.length) return null;
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={toggleAriaLabel ?? t("rowActionsAria")}
          />
        }
      >
        <MoreVerticalIcon data-icon="inline-start" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          {items.map((item, i) =>
            item.isSeparator ? (
              <DropdownMenuSeparator key={i} />
            ) : (
              <DropdownMenuItem
                key={i}
                disabled={item.isDisabled}
                variant={item.isDanger ? "destructive" : "default"}
                onClick={item.onClick}
              >
                {item.title}
              </DropdownMenuItem>
            ),
          )}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
