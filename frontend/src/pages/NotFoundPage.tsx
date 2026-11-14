import { SearchIcon } from "lucide-react";
import {
  EmptyDescription,
  EmptyContent,
  Empty,
  EmptyHeader,
  EmptyTitle,
  EmptyMedia,
} from "@/components/ui/empty";
import { Button } from "@/components/ui/button";
import { useTranslation } from "react-i18next";

import { Link as RouterLink } from "react-router-dom";
import { usePageTitle } from "@/hooks/usePageTitle";

export function NotFoundPage() {
  const { t } = useTranslation("common");
  usePageTitle(t("errorPages.notFound.documentTitle"));
  return (
    <section className="page-section flex-1 min-h-0">
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <SearchIcon />
          </EmptyMedia>
          <EmptyTitle role="heading" aria-level={Number("h1".slice(1))}>
            {t("errorPages.notFound.title")}
          </EmptyTitle>
        </EmptyHeader>
        <EmptyDescription>{t("errorPages.notFound.body")}</EmptyDescription>
        <EmptyContent>
          <div className="flex flex-wrap justify-center gap-2">
            <Button asChild>
              <RouterLink to="/workspaces">
                {t("errorPages.goToDashboard")}
              </RouterLink>
            </Button>
          </div>
        </EmptyContent>
      </Empty>
    </section>
  );
}
