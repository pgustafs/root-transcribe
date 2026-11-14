import trilogin from "@/assets/brand/trilogin-trefargad.svg"
import triloginLight from "@/assets/brand/trilogin-trefargad-ljus.svg"
import { cn } from "@/lib/utils"

/**
 * The agent's picture: the enrooted trilogin (original files, unmodified), the
 * light-background variant in light mode and the dark-background one in dark
 * mode. Replaces the CUGA owl everywhere in this UI. It is never rotated here;
 * the rotating one is the loading <Spinner>.
 */
export function AgentMark({ className }: { className?: string }) {
  return (
    <span data-slot="agent-mark" aria-hidden className={cn("inline-flex size-8 shrink-0", className)}>
      <img src={triloginLight} alt="" className="size-full dark:hidden" />
      <img src={trilogin} alt="" className="hidden size-full dark:block" />
    </span>
  )
}
