import { useState } from "react";
import type { ReactNode } from "react";
import { CopyIcon, CheckIcon } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
export function CopyValue({
  children,
  "aria-label": label,
}: {
  children: ReactNode;
  "aria-label"?: string;
}) {
  const { t } = useTranslation("common");
  const [copied, setCopied] = useState(false);
  return (
    <span className="inline-flex max-w-full items-center gap-1">
      <code
        className="max-w-40 truncate text-xs"
        title={String(children ?? "")}
      >
        {children}
      </code>
      <Button
        variant="ghost"
        size="icon-xs"

        aria-label={copied ? t("actions.copied") : (label ?? t("actions.copy"))}
        onClick={() => {
          void navigator.clipboard
            .writeText(String(children ?? ""))
            .then(() => setCopied(true))
            .catch(() => setCopied(false));
        }}
      >
        {copied ? (
          <CheckIcon data-icon="inline-start" />
        ) : (
          <CopyIcon data-icon="inline-start" />
        )}
      </Button>
    </span>
  );
}
