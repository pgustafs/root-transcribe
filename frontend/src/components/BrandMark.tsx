import { Link } from "react-router";
import trilogin from "@/assets/brand/trilogin-trefargad.svg";
import triloginLight from "@/assets/brand/trilogin-trefargad-ljus.svg";
import lockupOnDark from "@/assets/brand/enrooted-logotyp-mork-bakgrund.svg";
import lockupOnLight from "@/assets/brand/enrooted-logotyp-ljus-bakgrund.svg";
import { cn } from "@/lib/utils";

/**
 * The app's picture: the enrooted trilogin (original files, unmodified), the
 * light-background variant in light mode and the dark one in dark mode. It is
 * never rotated here; the rotating one is the loading <Spinner>.
 */
export function BrandMark({ className }: { className?: string }) {
  return (
    <span
      data-slot="brand-mark"
      aria-hidden
      className={cn("inline-flex size-8 shrink-0", className)}
    >
      <img src={triloginLight} alt="" className="size-full dark:hidden" />
      <img src={trilogin} alt="" className="hidden size-full dark:block" />
    </span>
  );
}

/** enrooted lockup, original files, one per background, never under 120 px wide. */
export function Lockup({
  to = "/",
  className,
}: {
  to?: string;
  className?: string;
}) {
  const imgs = (
    <>
      <img
        src={lockupOnLight}
        alt="enrooted"
        className="h-[22px] w-auto min-w-[120px] dark:hidden"
      />
      <img
        src={lockupOnDark}
        alt="enrooted"
        className="hidden h-[22px] w-auto min-w-[120px] dark:block"
      />
    </>
  );
  return (
    <Link
      to={to}
      aria-label="enrooted"
      className={cn("flex shrink-0 items-center rounded-sm", className)}
    >
      {imgs}
    </Link>
  );
}
