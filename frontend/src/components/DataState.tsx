import {
  LayersIcon as CubesIcon,
  CircleAlertIcon as ExclamationCircleIcon,
  SearchIcon,
} from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import {
  EmptyDescription,
  EmptyContent,
  Empty,
  EmptyHeader,
  EmptyTitle,
  EmptyMedia,
} from "@/components/ui/empty";
import { Button } from "@/components/ui/button";
import { type ComponentType, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { getErrorMessage } from "@/api/client";

type DataStateProps = {
  isLoading: boolean;
  error?: unknown;
  isEmpty?: boolean;
  emptyTitle?: string;
  emptyBody?: ReactNode;
  /** Icon for the empty state; defaults to CubesIcon (SearchIcon when `isFiltered`). */
  emptyIcon?: ComponentType;
  /** Primary action for the empty state (e.g. a Create button). */
  emptyAction?: ReactNode;
  /** True when a filter/search produced the empty result; changes wording and icon. */
  isFiltered?: boolean;
  /** Called from the "Clear all filters" link in the filtered empty state. */
  onClearFilters?: () => void;
  /** Called from the "Try again" button in the error state. */
  onRetry?: () => void;
  loadingLabel?: string;
  /** Render skeleton rows instead of a spinner while loading. */
  skeleton?: { rows?: number; height?: string };
  /** Heading level of the state titles; use "h3" inside an h2 section. */
  headingLevel?: "h2" | "h3" | "h4";
  children?: ReactNode;
};

export function DataState({
  isLoading,
  error,
  isEmpty = false,
  emptyTitle,
  emptyBody,
  emptyIcon,
  emptyAction,
  isFiltered = false,
  onClearFilters,
  onRetry,
  loadingLabel,
  skeleton,
  headingLevel = "h2",
  children,
}: DataStateProps) {
  const { t } = useTranslation("common");
  const loadingText = loadingLabel ?? t("dataState.loading");

  if (isLoading) {
    if (skeleton) {
      const rows = skeleton.rows ?? 4;
      return (
        <div
          aria-busy="true"
          aria-label={loadingText}
          className="flex flex-col gap-4"
        >
          {Array.from({ length: rows }, (_, i) => (
            <div key={i}>
              <Skeleton className="h-5 w-full" />
            </div>
          ))}
        </div>
      );
    }
    return (
      <div className="flex min-h-40 items-center justify-center">
        <Spinner aria-label={loadingText} />
      </div>
    );
  }

  if (error) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <ExclamationCircleIcon />
          </EmptyMedia>
          <EmptyTitle role="heading" aria-level={Number(headingLevel.slice(1))}>
            {t("dataState.errorTitle")}
          </EmptyTitle>
        </EmptyHeader>
        <EmptyDescription>{getErrorMessage(error)}</EmptyDescription>
        {onRetry && (
          <EmptyContent>
            <div className="flex flex-wrap justify-center gap-2">
              <Button onClick={onRetry} variant="outline">
                {t("dataState.retry")}
              </Button>
            </div>
          </EmptyContent>
        )}
      </Empty>
    );
  }

  if (isEmpty) {
    // A filtered result always gets the "no match" presentation, regardless of
    // the object-specific empty-state props, so callers can pass both.
    const Icon = isFiltered ? SearchIcon : (emptyIcon ?? CubesIcon);
    const title = isFiltered
      ? t("dataState.filteredTitle")
      : (emptyTitle ?? t("dataState.emptyTitle"));
    const body = isFiltered
      ? t("dataState.filteredBody")
      : (emptyBody ?? t("dataState.emptyBody"));
    const action = isFiltered ? null : emptyAction;
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Icon />
          </EmptyMedia>
          <EmptyTitle role="heading" aria-level={Number(headingLevel.slice(1))}>
            {title}
          </EmptyTitle>
        </EmptyHeader>
        <EmptyDescription>{body}</EmptyDescription>
        {(action || (isFiltered && onClearFilters)) && (
          <EmptyContent>
            <div className="flex flex-wrap justify-center gap-2">
              {action}
              {isFiltered && onClearFilters && (
                <Button onClick={onClearFilters} variant="link">
                  {t("dataState.clearFilters")}
                </Button>
              )}
            </div>
          </EmptyContent>
        )}
      </Empty>
    );
  }

  return <>{children}</>;
}
