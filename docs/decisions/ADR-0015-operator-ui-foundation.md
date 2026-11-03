# ADR-0015: The operator UI's foundation

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M5 (M5.1, M5.2)

## Context

- **CLAUDE.md M5.1:** the app shell, routing, auth and an API client
  generated from OpenAPI, where hand-edited types fail CI.
- **CLAUDE.md M5.2:** design tokens per SRS 10.3, with no hard-coded colours
  outside the token file.
- **SRS 10:** the shell (10.4.0), restrained tokens (10.3), the five
  interaction states (10.8), the keyboard model (10.9), and WCAG 2.1 AA
  (UR-24).
- The stack is fixed by CLAUDE.md §3: React 18, Vite, TS strict, TanStack
  Query, Tailwind, shadcn/ui.

## Decision 1: one typed client, generated, and checked for drift

- `openapi-typescript` generates `frontend/src/api/schema.d.ts` from the
  committed `openapi.json`. `openapi-fetch` calls through it, so a path or
  field the server lacks is a compile error.
- `npm run api:check` regenerates the file into a temp directory and fails
  on any difference. A hand edit, or a schema change nobody regenerated, fails
  `make frontend` and CI's `frontend` job.
- `make openapi` now regenerates both halves.
- Screens use named types from `src/api/types.ts`, each taken from the
  generated schema.

## Decision 2: tokens in one file, and Tailwind can name nothing else

- `src/styles/tokens.css` holds every SRS 10.3 colour, size, radius and
  layout measure.
- `app.css` maps them into Tailwind and switches off Tailwind's default
  palette, type scale and radii. So `bg-red-500` and `text-2xl` don't exist:
  a class can only name an SRS token.
- `scripts/check-tokens.mjs` fails the lint step on any hex, `rgb()`, `hsl()`
  or arbitrary Tailwind colour outside `tokens.css`. That is proven to fail
  on a planted colour.

**One deliberate deviation from SRS 10.3.** `--text-muted` is `#5C6876`, not
`#6B7885`. The SRS value is 4.28:1 on `--surface` and 4.09:1 on the sidebar,
under the 4.5:1 that WCAG 2.1 AA requires and UR-24 makes a MUST. `#5C6876`
is the same hue at the smallest change that clears 4.5:1 on every surface
(4.57:1 at worst). With it, Lighthouse's accessibility score went from 96 to
100.

## Decision 3: "Apple-like" means craft, not space

The owner asked for an interface that is clean and easy, like Apple's. SRS
10.1 asks for density over whitespace (13px body, 40px rows), because
operators want more rows visible. SRS 10.3's numbers stand. The clarity comes
from:

- one system font;
- a strict type hierarchy (20 / 17 / 14 / 13 / 12 / 11);
- the 4px spacing scale;
- restrained colour that always means something (UR-31);
- tabular numerals (UR-32);
- quiet hover and focus states.

## Decision 4: the shell reads three endpoints, all scoped at the query layer

`GET /api/v1/me`, `/api/v1/work-queues` and `/api/v1/alerts/deadline-risk`
return no PHI, only counts and charges. They read through
`rcm.work.views`, which applies `scoped_query` (PD-20).

- The queues of SRS 10.2 are declared as groupings of work-item kinds. A test
  holds every kind in exactly one queue.
- A queue is listed only to a principal holding its permission.
- Queue counts and the deadline alert poll every 30s (UR-23).

The deadline bar (UR-26) has no dismiss control, and a test fails if one
appears. When the check itself fails, the bar says so instead of
disappearing (PD-18).

## Decision 5: development sign-in until OIDC is connected

- Production signs in at the IdP (S-3). That isn't wired yet; it needs the
  IdP's configuration.
- Until it is, `python -m tools.dev_token` mints a token from
  `RCM_DEV_JWT_SECRET`. The secret comes from the environment and is never
  written to a file.
- The sign-in screen accepts a pasted token, and the production build shows
  no paste form at all.
- The API and the token issuer both refuse dev tokens outside local and CI.
- The token lives in `sessionStorage`, so it ends with the tab (SR-14), and
  a 401 signs the operator out.

## Consequences

- `make verify` now includes `make frontend`: typecheck, lint plus tokens,
  client drift, Vitest, and the build.
- CI has a `frontend` job, and the verify gate needs it. The later-stage guard
  now shows Playwright/axe (M5 gate) as the only waiting UI stage.
- The first-load JavaScript is about 89 KB gzipped, against NFR-FE-04's
  300 KB budget.
