/*
 * enrooted glass app shell: icon rail · list panel · main · optional side panel.
 * Generic template from the enrooted-glass-ui skill. Copy it into a shadcn
 * (base-nova, Base UI) project and fill the slots; it has no app logic.
 *
 * The rail (views) and the list panel (items inside the open view) must show
 * different things. An app with only navigation should not use both: render the
 * rail when collapsed and the panel when expanded (references/design.md §4).
 *
 * Needs: src/index.css from the skill (tokens + .app-backdrop / .glass-chrome),
 * shadcn components button, tooltip, separator, sheet, and next-themes'
 * <ThemeProvider attribute="class"> around the app (see theme-toggle.tsx).
 */
import * as React from "react"
import { MenuIcon, PanelLeftCloseIcon, PanelLeftOpenIcon, PanelRightCloseIcon, PanelRightOpenIcon, PlusIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { AgentMark } from "@/components/agent-mark"
import { ThemeToggle } from "@/components/theme-toggle"
import lockupOnDark from "@/assets/brand/enrooted-logotyp-mork-bakgrund.svg"
import lockupOnLight from "@/assets/brand/enrooted-logotyp-ljus-bakgrund.svg"
import { cn } from "@/lib/utils"

const BP_DOCK_LEFT = 768 // below: rail + list live in a drawer
const BP_DOCK_RIGHT = 1100 // below: the side panel lives in a drawer

function useWindowWidth() {
  const [w, setW] = React.useState(window.innerWidth)
  React.useEffect(() => {
    const on = () => setW(window.innerWidth)
    window.addEventListener("resize", on)
    return () => window.removeEventListener("resize", on)
  }, [])
  return w
}

export interface RailView {
  label: string
  icon: React.ReactNode
  /** Either a link (leaves the SPA or routes) or a button. */
  href?: string
  onClick?: () => void
  selected?: boolean
}

/** One 36 px rail control with its name in a tooltip on the right. */
function RailItem({ view }: { view: RailView }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-lg"
            aria-label={view.label}
            aria-current={view.selected ? "page" : undefined}
            className={cn(
              "text-muted-foreground [&_svg:not([class*='size-'])]:size-[19px]",
              view.selected && "bg-foreground/8 text-foreground hover:bg-foreground/12"
            )}
            render={view.href ? <a href={view.href} /> : undefined}
            onClick={view.onClick}
          />
        }
      >
        {view.icon}
      </TooltipTrigger>
      <TooltipContent side="right">{view.label}</TooltipContent>
    </Tooltip>
  )
}

/** enrooted lockup, original files, one per background, never narrower than 120 px. */
export function Lockup() {
  return (
    <a href="/" aria-label="enrooted, home" className="flex shrink-0 items-center">
      <img src={lockupOnLight} alt="enrooted" className="h-[22px] w-auto min-w-[120px] dark:hidden" />
      <img src={lockupOnDark} alt="enrooted" className="hidden h-[22px] w-auto min-w-[120px] dark:block" />
    </a>
  )
}

export function AppShell({
  title,
  views,
  onNew,
  newLabel = "New",
  list,
  listLabel = "List",
  panel,
  panelLabel = "Details",
  railFooter,
  headerEnd,
  children,
}: {
  /** Title of what is open, shown in the glass title bar. */
  title: string
  /** Rail destinations under the divider. */
  views: RailView[]
  /** The accented + at the top of the rail. Omit to hide it. */
  onNew?: () => void
  newLabel?: string
  /** Content of the list panel next to the rail (below the lockup). */
  list: React.ReactNode
  listLabel?: string
  /** Optional right-hand side panel. */
  panel?: React.ReactNode
  panelLabel?: string
  /** Extra rail controls above the theme toggle (e.g. the account menu). */
  railFooter?: React.ReactNode
  /** Extra title-bar buttons, left of the side-panel toggle. */
  headerEnd?: React.ReactNode
  /** Main content. It scrolls under the title bar: start it with pt-12 (or pt-18 for text). */
  children: React.ReactNode
}) {
  const width = useWindowWidth()
  const leftDocked = width >= BP_DOCK_LEFT
  const rightDocked = width >= BP_DOCK_RIGHT
  const [leftOpen, setLeftOpen] = React.useState(leftDocked)
  const [rightOpen, setRightOpen] = React.useState(rightDocked && !!panel)

  const rail = (
    <nav aria-label="Main" className="flex w-13 shrink-0 flex-col items-center gap-2 border-r px-1 py-2">
      <AgentMark className="m-1 size-7" />
      {onNew && (
        <Tooltip>
          <TooltipTrigger render={<Button size="icon-lg" aria-label={newLabel} onClick={onNew} />}>
            <PlusIcon className="size-5" />
          </TooltipTrigger>
          <TooltipContent side="right">{newLabel}</TooltipContent>
        </Tooltip>
      )}
      <Separator className="my-1 w-7!" />
      {views.map((v) => (
        <RailItem key={v.label} view={v} />
      ))}
      <div className="mt-auto flex flex-col items-center gap-2">
        {railFooter}
        <ThemeToggle side="right" />
      </div>
    </nav>
  )

  const listPanel = (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-12 shrink-0 items-center justify-between gap-2 pr-2 pl-3">
        <Lockup />
        <Button variant="ghost" size="icon-sm" aria-label={`Close ${listLabel.toLowerCase()}`} className="text-muted-foreground" onClick={() => setLeftOpen(false)}>
          <PanelLeftCloseIcon />
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{list}</div>
    </div>
  )

  const iconButton = (label: string, icon: React.ReactNode, onClick: () => void) => (
    <Button variant="ghost" size="icon-sm" aria-label={label} className="text-muted-foreground" onClick={onClick}>
      {icon}
    </Button>
  )

  return (
    <div className="app-backdrop flex h-dvh">
      {leftDocked && <div className="glass-chrome flex shrink-0">{rail}</div>}
      {leftDocked && (
        <aside
          aria-label={listLabel}
          className={cn("glass-chrome shrink-0 overflow-hidden border-r transition-[width] duration-200", leftOpen ? "w-68" : "w-0 border-r-0")}
        >
          <div className="h-full w-68">{listPanel}</div>
        </aside>
      )}

      <main className="relative flex min-w-0 flex-1 flex-col">
        {/* Glass title bar: floats over the content, which scrolls underneath it. */}
        <div className="glass-chrome absolute inset-x-0 top-0 z-20 flex h-12 items-center gap-2 border-b px-3">
          {!leftDocked
            ? iconButton("Open navigation", <MenuIcon />, () => setLeftOpen(true))
            : !leftOpen && iconButton(`Open ${listLabel.toLowerCase()}`, <PanelLeftOpenIcon />, () => setLeftOpen(true))}
          <h1 className="min-w-0 truncate text-sm font-medium">{title}</h1>
          <div className="ml-auto flex shrink-0 items-center gap-0.5">
            {headerEnd}
            {panel &&
              iconButton(
                rightOpen ? `Close ${panelLabel.toLowerCase()}` : `Open ${panelLabel.toLowerCase()}`,
                rightOpen ? <PanelRightCloseIcon /> : <PanelRightOpenIcon />,
                () => setRightOpen((v) => !v)
              )}
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </main>

      {panel && rightDocked && (
        <aside
          aria-label={panelLabel}
          className={cn("glass-chrome shrink-0 overflow-hidden border-l transition-[width] duration-200", rightOpen ? "w-[26rem]" : "w-0 border-l-0")}
        >
          <div className="h-full w-[26rem] overflow-y-auto">{panel}</div>
        </aside>
      )}

      {/* Phones: rail and list share one drawer. initialFocus={false} keeps the first rail tooltip closed. */}
      {!leftDocked && (
        <Sheet open={leftOpen} onOpenChange={setLeftOpen}>
          <SheetContent side="left" showCloseButton={false} initialFocus={false} className="w-[21rem] max-w-[92vw] flex-row gap-0 p-0">
            <SheetTitle className="sr-only">Navigation</SheetTitle>
            <SheetDescription className="sr-only">{listLabel}</SheetDescription>
            {rail}
            <div className="min-w-0 flex-1">{listPanel}</div>
          </SheetContent>
        </Sheet>
      )}
      {panel && !rightDocked && (
        <Sheet open={rightOpen} onOpenChange={setRightOpen}>
          <SheetContent side="right" showCloseButton={false} className="w-full gap-0 p-0 sm:max-w-[26rem]">
            <SheetTitle className="sr-only">{panelLabel}</SheetTitle>
            <SheetDescription className="sr-only">{panelLabel}</SheetDescription>
            {panel}
          </SheetContent>
        </Sheet>
      )}
    </div>
  )
}
