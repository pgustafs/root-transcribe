import type { ReactNode } from "react";
import { usePageTitle } from "@/hooks/usePageTitle";
type Props = {
  title: string;
  documentTitle?: string;
  description?: ReactNode;
  actions?: ReactNode;
  labels?: ReactNode;
  icon?: ReactNode;
  breadcrumb?: ReactNode;
  children?: ReactNode;
};
export function PageHeader({
  title,
  documentTitle,
  description,
  actions,
  labels,
  icon,
  breadcrumb,
  children,
}: Props) {
  usePageTitle(documentTitle ?? title);
  return (
    <header className="page-heading">
      {breadcrumb && <div className="mb-3">{breadcrumb}</div>}
      <div className="flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-3">
            {icon}
            <h1 className="page-title">{title}</h1>
            {labels}
          </div>
          {description && (
            <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
              {description}
            </p>
          )}
        </div>
        {actions && (
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
        )}
      </div>
      {children}
    </header>
  );
}
