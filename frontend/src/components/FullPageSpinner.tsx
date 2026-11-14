import { Spinner } from "@/components/ui/spinner";
import { useTranslation } from "react-i18next";

export function FullPageSpinner({ label }: { label?: string }) {
  const { t } = useTranslation("common");
  return (
    <div className="flex min-h-40 items-center justify-center">
      <Spinner
        className="size-8"
        aria-label={label ?? t("dataState.loading")}
      />
    </div>
  );
}
