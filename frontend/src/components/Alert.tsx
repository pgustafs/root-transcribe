import { cn } from "@/lib/utils";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  CircleAlertIcon,
  CircleCheckIcon,
  InfoIcon,
  TriangleAlertIcon,
} from "lucide-react";
import {
  Alert as Callout,
  AlertTitle,
  AlertDescription,
} from "@/components/ui/alert";

type Props = {
  variant?: "custom" | "info" | "success" | "warning" | "danger";
  title: ReactNode;
  children?: ReactNode;
  actionClose?: ReactNode;
  isInline?: boolean;
  isLiveRegion?: boolean;
  className?: string;
};
export function Alert({
  variant = "info",
  title,
  children,
  actionClose,
  isLiveRegion,
  className,
}: Props) {
  const { t } = useTranslation("common");
  const Icon =
    variant === "danger"
      ? CircleAlertIcon
      : variant === "success"
        ? CircleCheckIcon
        : variant === "warning"
          ? TriangleAlertIcon
          : InfoIcon;
  return (
    <Callout
      variant={variant === "danger" ? "destructive" : "default"}
      role={isLiveRegion ? "alert" : "status"}
      className={cn(className, actionClose && "pr-12")}
    >
      <Icon />
      <AlertTitle>
        <span className="sr-only">{t(`alert.${variant}`)} </span>
        {title}
      </AlertTitle>
      {children && <AlertDescription>{children}</AlertDescription>}
      {actionClose && (
        <div className="absolute right-2 top-2">{actionClose}</div>
      )}
    </Callout>
  );
}
