# Root Transcribe frontend

React 19, TypeScript and shadcn/ui provide recording workspaces, transcript review,
speaker profiles, account management and job status screens. React Query owns
server state; Axios uses Keycloak access tokens and same-origin `/api/v1` URLs.
See the [architecture](../ARCHITECTURE.md) and [setup guide](../README.md).

## Development

Use Node.js 24+ and npm. From this directory:

```sh
npm ci
npm run dev
```

Vite serves `http://localhost:3000` and proxies `/api` to port 8000. The backend
and its backing services must be configured separately. The Keycloak client
must allow the frontend origin. The realm template includes development origins;
the deployment generator instead sets the origin selected in `APP_URL`.
Production builds use [nginx](../deploy/frontend-nginx.conf) to serve static assets
and proxy the API. The application shell is revalidated; hashed assets are cached.

## Pages and state

| Route | Purpose |
| --- | --- |
| `/login` | Redirect to Keycloak sign-in |
| `/workspaces` | Workspace list and creation |
| `/workspaces/:id` or `/workspaces/:id/meetings` | Recordings, playback, transcript review and workspace controls |
| `/admin/speakers` | Speaker directory; administrators and power users |
| `/admin/jobs` | Background job monitor; administrators |
| `/users` | Local account administration; administrators |
| `/profile` | Personal profile and identity/account links |
| `/forbidden` and unmatched paths | Access-denied and not-found pages |

Workspace roles and audio capabilities control available actions. The backend
checks permissions independently. Pending work is polled; transcript editing
uses revision and segment versions. The native audio player supports playback
and seeking through authorized streaming URLs issued by the API.

`RecordingProvider` manages capture across route changes. `recordingStore.ts`
persists chunks and recovery metadata in IndexedDB, and `uploadManager.ts` sends
resumable uploads. File-picker uploads use the ordinary multipart API. The local
copy remains available when server-side validation fails.

The interface supports English and Swedish independently of **Recording language:
Auto / English / Swedish**. That control appears before recording or file upload;
its choice is persisted and reused after local recovery and server retries.
Auto omits the ASR language hint. Existing recordings have Auto as their saved
preference, and there is no preference editor for already uploaded recordings.

## Components and branding

Application-owned UI primitives live in `src/components/ui/` and use Radix,
Tailwind CSS v4 and Lucide icons. Their MIT notice is retained in
[src/components/ui/LICENSE](src/components/ui/LICENSE). `components.json`
configures the shadcn CLI; run `npx shadcn@latest add <component>` here when adding
a primitive. Theme tokens and transcript layouts live in `src/index.css`.

Branding follows the
[Enrooted installer guide](https://enrooted.github.io/root-ai-k3s/presentations/installer-guide.html):
navy `#0D1835`, turquoise `#00C2CB`, mist `#F4F6FA`, and green/orange accents.
[BrandMark.tsx](src/components/BrandMark.tsx) renders the three-arc mark and the
favicon is bundled in `public/`. In dark mode, the header and sidebar share
`#09122A`, the page uses `#0D1835`, and cards use `#13204A`.
Poppins headings and Lato body text are bundled from `src/assets/fonts/`, JetBrains
Mono through Fontsource; their licenses are under `public/fonts/`. Small accent text uses a
darker turquoise in light mode for contrast. Components use semantic theme tokens.

## Verification

```sh
npm run typecheck
npm run lint
npm run format:check
npm test -- --maxWorkers=1
npm run build
```

Vitest uses jsdom, MSW and fake IndexedDB. It checks recording recovery, upload
contracts, language selection, transcript interactions and other UI behavior
without running speech models. ESLint currently reports eight existing warnings
in `RecordingProvider.tsx`; they are separate from lint errors.

For browser checks, install Chromium once:

```sh
npx playwright install chromium
```

`npm run test:e2e` checks that the sign-in page and login button are available
against a running deployment (default `http://localhost:8095`, override with
`E2E_BASE_URL`). It does not perform an authenticated recording or prove inference.

For an authenticated visual audit, run `node scripts/visual-audit.mjs` here on a
host with Chromium support. It captures sign-in, workspaces, an existing recording
(including review tabs and actions), speakers, users, jobs, profile and error pages
in desktop, dark and mobile layouts. It checks playback/seeking, player visibility,
accessibility findings and horizontal overflow. Failed checks produce a nonzero
exit status. Provide a real existing playable recording for the playback check.

Screenshots and `report.json` go to ignored `test-results/visual-audit/`. The script
uses `admin@exampel.com` by default and reads `KEYCLOAK_APP_ADMIN_PASSWORD` from
`deploy/secrets.local.json`. Set `E2E_USERNAME` and `E2E_PASSWORD` for another
account or a password changed after initial setup; set `E2E_BASE_URL` for another
deployment. The default username is a local audit convention, not the generator's
`admin@example.com` default. Do not commit credentials or private screenshots.
