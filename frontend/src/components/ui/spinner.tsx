import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import trilogin from "@/assets/brand/trilogin-trefargad.svg";
import triloginLight from "@/assets/brand/trilogin-trefargad-ljus.svg";

/**
 * enrooted loading indicator: the tri-colour trilogin turning 360° in 2.4 s,
 * linear. The light-background variant is used in light mode.
 */
function Spinner({ className, ...props }: React.ComponentProps<"span">) {
  const { t } = useTranslation("common");
  return (
    <span
      data-slot="spinner"
      role="status"
      aria-label={t("dataState.loading")}
      className={cn("inline-flex size-4 shrink-0", className)}
      {...props}
    >
      <img
        src={triloginLight}
        alt=""
        className="enrooted-spin size-full dark:hidden"
      />
      <img
        src={trilogin}
        alt=""
        className="enrooted-spin hidden size-full dark:block"
      />
    </span>
  );
}

export { Spinner };
