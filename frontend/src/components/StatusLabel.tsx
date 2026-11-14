import {
  CircleCheckIcon,
  CircleAlertIcon,
  InfoIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { useTranslation } from "react-i18next";
import { humanize, type StatusPresentation } from "@/lib/labels";
type Props = {
  presentation?: StatusPresentation;
  value?: string;
  isCompact?: boolean;
  title?: string;
};
export function StatusLabel({ presentation, value, title }: Props) {
  const { t } = useTranslation("common");
  const text = presentation
    ? t(presentation.labelKey)
    : value
      ? humanize(value)
      : t("unknownValue");
  const tone = presentation?.tone;
  const status = tone && "status" in tone ? tone.status : undefined;
  const Icon =
    status === "danger"
      ? CircleAlertIcon
      : status === "success"
        ? CircleCheckIcon
        : status === "warning"
          ? TriangleAlertIcon
          : InfoIcon;
  return (
    <Badge
      variant={
        status === "danger"
          ? "destructive"
          : status === "success"
            ? "success"
            : status === "warning"
              ? "warning"
              : "secondary"
      }
      title={title}
    >
      {presentation?.inProgress ? (
        <Spinner aria-hidden="true" />
      ) : status ? (
        <Icon aria-hidden="true" />
      ) : null}
      {text}
    </Badge>
  );
}
