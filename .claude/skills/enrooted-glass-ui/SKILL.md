---
name: enrooted-glass-ui
description: Build or change a web UI in the enrooted glass design used by the CUGA chat UI (apps/chat-ui) - shadcn/ui on Base UI, enrooted colours and fonts, OpenClaw-style layout (icon rail, list panel, centred chat column, floating composer), frosted-glass surfaces, trilogin as the agent picture, light/dark switch. Use whenever the user wants a new page, panel, dashboard, app UI or frontend container in this repo, wants to restyle or extend the chat UI, or says "our design", "same look as the chat UI", "enrooted style", "glass", "like openclaw" - even if they don't name shadcn or the design. Also use for any change to apps/chat-ui's look or layout.
---

# enrooted glass UI

One design for every UI in this repo: enrooted's colours, fonts and marks, on shadcn/ui
components, laid out like OpenClaw's Control UI, with frosted-glass chrome.

**Reference implementation (verified working):** `apps/chat-ui/` (chat and the agent
Manage pages), written up in
`03-chat-ui.md` with screenshots in `docs/images/03-chat-ui/`. Look at those screenshots
first: they show the target better than any description. When in doubt, copy from
`apps/chat-ui/src`. The files in `templates/` are copies of its shared parts, plus a
generic app shell.

**Before choosing a colour, surface or measurement, read `references/design.md`.** It has
the token table, the layout measurements, the glass rules, the brand rules and the
pitfalls already hit. Read `ui/skills/shadcn/SKILL.md` too when adding or composing shadcn
components: this project is the **Base UI** flavour (`render` props, not `asChild`).

## The design in six lines

1. **Colours:** navy `#0D1835`, white, light grey, and turquoise `#00C2CB` as the single
   accent. Green and orange are status colours only. Always through shadcn tokens.
2. **Fonts:** Poppins for headings and controls, Lato for text. Bundled, no CDN.
3. **Layout:** 52 px icon rail · 272 px list panel · centred content column under a 48 px
   title bar · optional 416 px side panel. No top navigation bar. An app with only
   navigation (nothing to list per view) gets one sidebar that is the rail when collapsed
   and the panel when expanded, never both.
4. **Glass:** rail, panels and title bar are `.glass-chrome`; the composer and floating
   lists are `.glass-card`; menus and dialogs are glass automatically.
5. **Marks:** the enrooted lockup at the top of the list panel; the three-colour trilogin
   is the agent's picture and (rotating) the only spinner. No other logos.
6. **Light and dark:** light = white theme, dark = navy theme, switched from the rail.

## Workflow A: change the existing chat UI

1. Edit under `apps/chat-ui/src`. Keep logic (`lib/`, `hooks/`) and look (`components/`,
   `index.css`) apart: a design change should not need to touch the stream handling.
2. Check: `scripts/check.sh apps/chat-ui` → must print `CHECK_OK`.
3. Deploy without restarting the agent:
   `scripts/hot_deploy.sh apps/chat-ui localhost/cuga-chat-ui:latest cuga-chat-ui`.
   `podman kube play --replace` would re-create the whole pod and cut off running
   conversations, so only use it for manifest changes. An `nginx.conf` change can go live
   without it: `podman cp apps/chat-ui/nginx.conf cuga-chat-ui:/etc/nginx/conf.d/default.conf
   && podman exec cuga-chat-ui nginx -t && podman exec cuga-chat-ui nginx -s reload`.
4. Verify in a real browser (see "Verify" below), in **both** themes.
5. If the theme, brand assets or shared components changed, run
   `scripts/sync_templates.sh` so this skill's templates stay equal to the app.
6. Update `03-chat-ui.md` and re-run `docs/e2e/03-chat-ui.py` for fresh screenshots.

## Workflow B: a new UI with the same design

1. **Create the project** (no Node on the host, so the shadcn CLI runs in a container):

   ```bash
   cd ~/cuga-podman/apps
   podman run --rm -v $PWD:/work:Z -w /work docker.io/library/node:22 \
     npx -y shadcn@latest init --name <app> --template vite --base base --preset nova --no-monorepo -y
   cd <app>
   podman run --rm -v $PWD:/work:Z -w /work docker.io/library/node:22 sh -c \
     'npx -y shadcn@latest add button tooltip separator sheet dropdown-menu input-group avatar badge skeleton spinner empty sonner -y \
      && npm install next-themes'
   ```

   Add more components as the UI needs them. For chat: `message-scroller message bubble
   trace code-block attachment textarea`.

2. **Apply the theme:** `scripts/apply_theme.sh apps/<app>`. It copies `index.css`, the
   brand SVGs, the fonts, `agent-mark.tsx`, `theme-toggle.tsx` and the trilogin spinner,
   and renames `text-destructive` to `text-warn-text`. Re-run it after every later
   `shadcn add`.

3. **Wire it up by hand** (the script leaves these alone):
   - `index.html`: copy `templates/index.html` (favicon and the inline script that applies
     the saved theme before first paint). Set the `<title>` and a storage key of your own.
   - `App.tsx`: wrap the app in
     `<ThemeProvider attribute="class" defaultTheme="system" enableSystem storageKey="<same key>" disableTransitionOnChange>`
     and in `<TooltipProvider>`. The key must match the one in `index.html`.
   - Remove the `@fontsource-variable/geist` import if the CLI added one.

4. **Lay it out** with `templates/components/app-shell.tsx` (copy it to
   `src/components/`). It gives the rail, list panel, glass title bar, optional side panel
   and the mobile drawer, and takes the app's parts as props (`views`, `list`, `panel`,
   `children`). Give `list` content that differs from the rail (the items inside the open
   view). If there is none, don't repeat the rail's links there: build one collapsible
   sidebar as described in `references/design.md` §4. For a chat screen, copy the composer and message components from
   `apps/chat-ui/src/components/chat/` instead of rebuilding them.

5. **Container:** copy `apps/chat-ui/Containerfile`, `.containerignore` and `nginx.conf`
   and adapt the port and proxy target. Add the container to a pod manifest under `kube/`
   and publish it on `127.0.0.1` only, like the other UIs here.

6. Check, deploy, verify (as in workflow A), then document it as the next numbered
   tutorial step with screenshots.

## Rules worth knowing why

- **Tokens, not colours.** A hex value or `bg-blue-500` in a component breaks the theme
  switch and drifts from the brand. If a token is missing, add it to `index.css` in both
  `:root` and `.dark`.
- **One accent.** Turquoise marks the one main action on a surface (the rail **+**, send).
  If two things are turquoise, neither stands out; selection uses the navy tint.
- **Orange and green carry meaning** (needs action / OK), so they are never text colour
  or decoration. Errors: normal-colour words plus an orange icon or border.
- **Glass only where it is defined** (`.glass-chrome`, `.glass-card`, overlays), and never
  with `opacity` on the same element: the text behind shows through unblurred.
- **Brand files stay untouched.** No recolouring, redrawing or cropping. The lockup is
  never narrower than 120 px, which is why it lives in the list panel and not on the rail.
- **Every icon-only control** gets an `aria-label` and a tooltip.
- **No internet at runtime.** Fonts and marks are bundled; don't add CDN links.

The user asked for two things that go beyond the written enrooted identity: glass
surfaces, and the trilogin used alone as the agent's picture. Keep both; don't "correct"
them back to flat or to another avatar.

## Verify

A design change isn't done until it has been looked at. Type checks don't see a greyed-out
composer or unreadable text.

1. `scripts/check.sh <project>` prints `CHECK_OK`.
2. Take screenshots with Playwright inside the pod (the CUGA image ships Chromium), at
   1440×900 in light and dark, plus 390×844 for mobile. Pattern: `docs/e2e/03-chat-ui.py`.

   ```bash
   podman run --rm --restart=no --pod cuga -v $PWD/<script>.py:/s.py:ro,Z -v <out dir>:/t:Z \
     --entrypoint /app/.venv/bin/python localhost/cuga-agent:slim /s.py
   ```

   Always pass `--restart=no`: a container added to a pod inherits its "always restart"
   policy and would re-run the script in a loop.
3. **Read the screenshots** and check: both themes; the lockup and trilogin swap per theme;
   text behind the composer is blurred; nothing is turquoise except the main action; the
   empty state, a long scrolled page and the mobile drawer all look right.
4. For the chat UI also run `apps/chat-ui/e2e/theme.py` (no model calls) and, after
   changes to chat behaviour, `apps/chat-ui/e2e/e2e.py` (real model; it needs the temporary
   approval policy described in `03-chat-ui.md`, removed again afterwards).
5. After changes to the Manage pages (`src/pages/manage-*.tsx`, `src/components/manage/`,
   `src/lib/manage/`) run `apps/chat-ui/e2e/manage.py`. It works on a throwaway agent and
   checks that every other agent's config is byte-identical afterwards.

## Pages that write configuration

The Manage pages save to the agents the user relies on, so a UI bug there can damage real
settings. When porting or changing such a page:

- Test on a throwaway agent, never on an existing one, and snapshot the other agents'
  configs before and after.
- Compare requests with the stock UI (port 7860): do the same edit in both and log every
  non-GET request. They should match. This caught a draft being re-sent on every render.
- Keep ported logic (state, autosave, publish) as close to upstream as possible and
  rewrite only the view; `manage-page.tsx` is split that way at the `// ── view` line.
- Callbacks passed into the save hooks must keep one identity across renders
  (`useCallback`): the hooks list them as effect dependencies.

## Files in this skill

| Path | What |
|---|---|
| `references/design.md` | tokens, typography, marks, layout measurements, glass rules, pitfalls |
| `templates/index.css` | the whole theme: fonts, light/dark tokens, glass classes |
| `templates/index.html` | favicon + pre-paint theme script |
| `templates/components/app-shell.tsx` | generic rail · list · title bar · side panel · mobile drawer |
| `templates/components/agent-mark.tsx`, `theme-toggle.tsx`, `ui/spinner.tsx` | trilogin picture, Light/Dark/System menu, rotating trilogin |
| `templates/assets/` | original enrooted SVGs and Poppins/Lato font files |
| `scripts/apply_theme.sh` | put the theme into a shadcn project |
| `scripts/check.sh` | typecheck + lint in a node container |
| `scripts/hot_deploy.sh` | build and swap static files in the running container |
| `scripts/sync_templates.sh` | refresh `templates/` from `apps/chat-ui` |
