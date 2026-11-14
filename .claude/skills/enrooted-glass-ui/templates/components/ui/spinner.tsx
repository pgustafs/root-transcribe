import { cn } from "cn"

import trilogin from "@/assets/brand/trilogin-trefargad.svg"
import triloginLight from "@/assets/brand/trilogin-trefargad-ljus.svg"

/**
 * enrooted loading indicator (TRI-3): the tri-colour trilogin turning 360° in
 * 2.4 s, linear. The light-background variant is used in light mode.
 */
function Spinner({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span data-slot="spinner" role="status" aria-label="Loading" className={cn("inline-flex size-4 shrink-0", className)} {...props}>
      <img src={triloginLight} alt="" className="enrooted-spin size-full dark:hidden" />
      <img src={trilogin} alt="" className="enrooted-spin hidden size-full dark:block" />
    </span>
  )
}

export { Spinner }
