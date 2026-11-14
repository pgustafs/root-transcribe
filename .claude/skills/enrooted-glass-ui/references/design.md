# enrooted glass UI: design reference

Read this before choosing a colour, a surface or a layout measurement. Everything here is
implemented in `templates/index.css` and `templates/components/`; this file says what each
part means and why. Source of the brand values: enrooted grafisk identitet 2026-10-29v2.

Contents
1. Colour tokens (light and dark)
2. Typography
3. Brand marks
4. Layout zones and measurements
5. Glass surfaces
6. Brand rules that shape code
7. Pitfalls already hit (and their fixes)

## 1. Colour tokens

Use shadcn's semantic tokens only (`bg-background`, `text-foreground`, `border-border` …).
Never write a hex value or a Tailwind palette colour (`bg-blue-500`) in a component: the
theme switch only works through the tokens.

| Token | Light (enrooted "vit") | Dark (enrooted "navy") |
|---|---|---|
| `background`, `card`, `popover` | white `#FFFFFF` | navy `#0D1835` |
| `foreground` (headings, labels) | navy `#0D1835` | white |
| `body-text` → `text-body` (long text) | navy 80 % `#3D4560` | white |
| `muted-foreground` (secondary text) | `#3D4560` | `#A8B2CA` |
| `secondary`, `muted`, `sidebar` | light grey `#F4F6FA` | `#1A2542` (derived raised navy) |
| `primary` (the one accent) | turquoise `#00C2CB`, navy text | same |
| `accent` (hover tone) | turquoise 8 % | turquoise 12 % |
| `border`, `input` | navy 20 % `#CFD3DD` | navy 80 % `#3D4560` |
| `ring`, `link` → `text-link` | `#00787E` | `#00C2CB` |
| `accent-line` | `#00A0A8` | `#00C2CB` |
| `destructive`, `status-warn` | orange `#FF7A00` | same |
| `status-ok` | green `#22C55E` | same |
| `chart-1/2/3` | turquoise, green, orange (in that order) | same |

Selected rows and rail items: `bg-foreground/8` (navy tint in light, white tint in dark).
Hover on rows: `bg-foreground/5`.

## 2. Typography

- **Poppins** 500/600: headings, shadcn `Button`s, tabs, labels, badges, dialog titles.
  Applied by `data-slot` in `index.css`, so it follows the components automatically.
- **Lato** 400/700 (+ italic): everything else. Raw `<button>`s that wrap content (list
  rows, step rows) deliberately stay Lato: Poppins looks heavy in dense lists.
- Only these weights exist. `font-medium` on Lato renders 400 and `font-semibold` 700.
- Code: `font-mono` (JetBrains Mono if installed, else system monospace; not bundled).
- **Eyebrow** (section labels such as "TODAY", "RECENT"):
  `text-[0.6875rem] font-medium tracking-[0.1em] text-link uppercase` on an `h3`.
- Fonts are bundled TTFs (`src/assets/fonts`), so the UI works without internet access.

## 3. Brand marks

| Mark | File(s) | Where |
|---|---|---|
| Lockup (symbol + "enrooted") | `enrooted-logotyp-ljus-bakgrund.svg` (light), `…-mork-bakgrund.svg` (dark) | top of the list panel and the mobile drawer; 22 px high, never under 120 px wide |
| Trilogin (three-colour symbol) | `trilogin-trefargad-ljus.svg` (light), `trilogin-trefargad.svg` (dark) | the agent's / app's picture: rail top, empty-state hero (48 px), next to answers (28 px). Component: `AgentMark` |
| Rotating trilogin | same files | the only loading spinner (`Spinner`): 360° in 2.4 s, linear |
| Favicon | `favicon-trefargad.svg` | `index.html` |

Use the original files unmodified: don't recolour, redraw, crop into a circle or set the
wordmark as text. Swap light/dark with two `<img>`s (`dark:hidden` / `hidden dark:block`).
Don't put a product's own logo (for example the CUGA owl) or name next to the lockup.
Write the name in lower case: enrooted.

## 4. Layout zones and measurements

```
┌────┬──────────────┬───────────────────────────────────────┬──────────────┐
│rail│ list panel   │ title bar (glass, floats, 48 px)      │ side panel   │
│52px│ 272 px       ├───────────────────────────────────────┤ 416 px       │
│    │ lockup       │ content, one centred column           │ (optional)   │
│ ◯  │ search       │ max-w-3xl (768 px); scrolls under the │              │
│ +  │ EYEBROW      │ title bar and the composer            │              │
│ ── │ row      2m  │                                       │              │
│ ▢  │ row      5d  │ ┌───────────────────────────────────┐ │              │
│ ▢  │              │ │ composer card (glass, floats)     │ │              │
│ ☼  │ count   🗑   │ └───────────────────────────────────┘ │              │
└────┴──────────────┴───────────────────────────────────────┴──────────────┘
```

- **Rail** `w-13`: 36 px controls (`size="icon-lg"`), 19 px icons, `gap-2`. Top to bottom:
  trilogin (opens the agent/app menu) · primary **+** · separator · views · (spacer) ·
  theme · account. Every control has a tooltip on the right and an `aria-label`.
- **List panel** `w-68`: 48 px header with the lockup and a close button; search
  (`InputGroup`); eyebrow group labels; rows `h-8 rounded-md px-2`, title truncated, short
  age right-aligned (`now`, `5m`, `3h`, `2d`, `Nov 3`); 40 px footer with count and bulk action.
- **Rail and list panel show different things.** The rail switches between views; the
  list panel lists what is inside the open view (conversations, agents, files). If an app
  has only navigation and nothing to list, the two would show the same links twice, which
  reads as two menus. Then use **one** sidebar instead: the 52 px rail when collapsed, the
  272 px panel (lockup, labelled rows, account + theme in a footer) when expanded, never
  both side by side, and only the panel in the mobile drawer. Example:
  `frontend/src/components/AppLayout.tsx` in root-transcribe.
- **Title bar** `h-12`, `absolute inset-x-0 top-0 z-20 glass-chrome border-b`: optional
  nav button, title (`text-sm font-medium`, truncated), badges, then icon buttons.
- **Content**: `mx-auto max-w-3xl px-4`, top padding `pt-18` so the first line clears the bar.
- **Composer**: `glass-card rounded-xl`, textarea `min-h-14 px-3.5 py-3`, a control row
  below (left: attach + key hint in `text-xs text-muted-foreground`; right: round primary
  send/stop `size-8 rounded-full`). Floats at the bottom (`absolute bottom-0 pb-4`).
- **Empty state**: trilogin 48 px, `h2 text-2xl font-semibold`, the composer in the middle
  (`max-w-2xl`), suggestion chips (`variant="outline" size="sm" rounded-full`), then a
  "RECENT" eyebrow list.
- **Messages**: the user's in a right-aligned `secondary` bubble (`rounded-2xl
  rounded-br-sm`); the agent's as plain text, with the trilogin at the top left of the turn
  and name + time above. Work/tool steps: collapsed "Show details · N steps".
- **Breakpoints**: under 768 px rail + list share one left drawer (a `Sheet`); under
  1100 px the side panel becomes a right drawer.
- **Radii**: `--radius: 0.5rem` → buttons 8 px, cards/composer about 12 px (`rounded-xl`), chips round.

## 5. Glass surfaces

Glass is a deliberate, user-requested exception to the identity's "flat" rule. Keep it to
these three uses so the page stays calm:

| Class / selector | Use | Fill |
|---|---|---|
| `.app-backdrop` | the app root, once | page colour + two soft washes (turquoise top right, green bottom left) |
| `.glass-chrome` | rail, list panel, side panel, title bar | `--sidebar` 70 % (light) / raised navy 55 % (dark) + `blur(12px) saturate(1.6)` |
| `.glass-card` | composer, floating lists | `--card` 72 % / raised navy 68 % + same blur |
| `[data-slot=…-content]` | menus, popovers, selects, dialogs, sheets | `--popover` 80 % + same blur (automatic) |

Rules:
- Glass needs something behind it. Chrome gets that from `.app-backdrop`; the title bar and
  composer get it by **floating over scrolling content** (absolute position, content padded).
- When content scrolls under a floating card whose height changes, measure the card with a
  `ResizeObserver` and feed the height to the content's bottom padding (`--composer-h`).
- Never set `opacity` (or `filter`) on a glass element or its ancestors: the text behind
  then shows through unblurred. Dim the children instead (`*:opacity-60`).
- Don't nest glass more than two levels, and don't invent new blur values.
- The glass rules are outside Tailwind's layers on purpose, so they beat `bg-*` utilities.
  A `bg-*` class on a `.glass-*` element has no effect.
- No backdrop-filter support → opaque fallback (already in `index.css`).

## 6. Brand rules that shape code

- **One accent.** Turquoise (`primary`) is for the main action only: the rail **+**, send,
  primary dialog buttons, focus rings, links. Selection is the navy tint, not turquoise.
- **Text on turquoise, green or orange is navy**, never white (`text-primary-foreground`).
- **Orange and green are status colours, never text and never decoration.** Orange =
  warning/error/needs action, green = OK. Show them as an icon, border or surface, always
  with words. Error text uses `text-warn-text` (normal text colour) next to a
  `text-status-warn` icon. `apply_theme.sh` renames shadcn's `text-destructive` for this.
- **Links** are `text-link underline`.
- **Bold, not colour, for emphasis** in running text.
- Motion: 120–200 ms, no decorative animation. The only rotation is the spinner.
- Focus rings are never removed.

## 7. Pitfalls already hit

| Symptom | Cause | Fix |
|---|---|---|
| Composer looks greyed out while empty | shadcn `InputGroup` dims itself when *any* child is disabled (the send button) | add `has-disabled:opacity-100`; dim only when the composer itself is disabled |
| Selected rail item shows no highlight | ghost `Button` has `aria-expanded:bg-muted`, which wins | add matching `aria-expanded:` classes, or use `aria-current`/`aria-pressed` instead |
| Opening the menu shows two left menus with the same links | rail and list panel both rendered the navigation, because the app has no per-view list | one collapsible sidebar: rail **or** panel, see §4 |
| Tooltip pops up when the mobile drawer opens | the dialog focuses the first rail control | `initialFocus={false}` on `SheetContent` |
| Text behind the composer is readable through it | `opacity` on the glass element | see §5 |
| Font rule doesn't reach a button | `DropdownMenuTrigger render={<Button/>}` replaces `data-slot="button"` | fine for icon buttons; add `font-heading` if it has text |
| Dark mode flashes white on reload | theme applied only after React mounts | keep the inline script in `index.html` (same storage key as `ThemeProvider`) |
| `asChild` does nothing | this is the Base UI flavour of shadcn | use `render={<a href… />}` |
| New `shadcn add` component shows orange text | it brought `text-destructive` back | re-run `apply_theme.sh` |
| Long dialog runs past the bottom, footer overlaps | the scrolling element is a `<fieldset>`, which does not shrink or scroll as a flex item | make a `div` the scroller (`min-h-0 flex-1 overflow-y-auto`) and put the fieldset inside; dialog itself `flex max-h-[90vh] flex-col` |
| Toast text is green or orange | sonner `richColors` | leave `richColors` off; `index.css` colours only the toast icon |
| A save request fires on every render | an inline arrow function passed to a hook that lists it as an effect dependency | wrap it in `useCallback` |
| `get_by_label` matches two elements in Playwright | Base UI checkboxes render a hidden input too; accordion panels are labelled by their trigger | select by role: `get_by_role("checkbox", name=…)`, `get_by_role("textbox", name=…)` |
