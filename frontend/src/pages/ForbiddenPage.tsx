import { LockKeyholeIcon as LockIcon } from "lucide-react";
import {
  EmptyDescription,
  EmptyContent,
  Empty,
  EmptyHeader,
  EmptyTitle,
  EmptyMedia,
} from "@/components/ui/empty";
import { buttonVariants } from "@/components/ui/button";
import { useTranslation } from "react-i18next";

import { Link as RouterLink } from "react-router";
import { usePageTitle } from "@/hooks/usePageTitle";

export function ForbiddenPage() {
  const { t } = useTranslation("common");
  usePageTitle(t("errorPages.forbidden.documentTitle"));
  return (
    <section className="page-section flex-1 min-h-0">
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <LockIcon />
          </EmptyMedia>
          <EmptyTitle role="heading" aria-level={Number("h1".slice(1))}>
            {t("errorPages.forbidden.title")}
          </EmptyTitle>
        </EmptyHeader>
        <EmptyDescription>{t("errorPages.forbidden.body")}</EmptyDescription>
        <EmptyContent>
          <div className="flex flex-wrap justify-center gap-2">
            <RouterLink to="/workspaces" className={buttonVariants()}>
              {t("errorPages.goToDashboard")}
            </RouterLink>
          </div>
        </EmptyContent>
      </Empty>
    </section>
  );
}
