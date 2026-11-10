# ADR-0023: GATE M5 — browser journeys and an axe scan of every screen

**Status:** Accepted · **Date:** 2026-11-10 · **Milestone:** M5 (gate)

## Context

- GATE M5 (CLAUDE.md §5) asks for two things: Playwright E2E over the five
  highest-volume operator journeys, and an axe scan that passes WCAG 2.1 AA.
- Vitest already covers every screen against a mocked API. A gate run against
  mocks would only re-prove the mocks. The point of this gate is the whole
  path: the browser, the real API, a real database and the real seed.
- `tools/check_ci_stages.py` had held an "E2E + accessibility" stage as
  pending since M0. It fails CI as soon as `frontend/playwright.config.ts`
  exists without a CI stage that runs it.

## Decision

**The five journeys** (`frontend/e2e/journeys.e2e.ts`) are the work an
operator repeats most often in a shift:

1. Sign in and pick up work from My Work.
2. Work an AR follow-up item by keyboard: take it, then resolve it with a
   reason (FR-WORK-04).
3. Look a claim up, read it, and jump to its decision audit (ADR-0022, M5.5).
4. Write off a denial as a deliberate second step (UR-37), then press `]` to
   go to the next one (UR-36).
5. Correct a denied claim and resubmit it after previewing every change
   (UR-10). The replacement must appear in the claim's submission chain
   (PD-7), and the member ID must be masked in the diff (S-6).

**The accessibility scan** (`frontend/e2e/accessibility.e2e.ts`) runs axe
with tags `wcag2a`, `wcag2aa`, `wcag21a` and `wcag21aa` on:

- every screen, scanned once its data has loaded: sign-in, My Work, three
  queues, search, claim detail, dashboard, rules, payer registry, not found;
- the denial workbench, with its decision panels;
- three dialogs, scanned while open: keyboard shortcuts, write-off, resolve.

Any violation fails the run and lists the rule and the elements that broke it.

**The database.** Every run uses `rcm_test`, never `rcm`.

- `tools/e2e_seed.py` wipes `rcm_test` and rebuilds it the way the system
  fills it: it loads the smoke corpus, advances claims, classifies and routes
  denials, publishes a ruleset and reviews scrubs. The queues are the ones an
  operator would see, not hand-made rows.
- The seed refuses any database other than `rcm_test`.
- Playwright starts its own API on port 8010 and its own UI on port 5183, so
  a developer's running stack is never reused by mistake.
- Each persona (AR specialist, denial specialist, operations manager) gets a
  development token, minted with the secret the API was started with. The
  tokens are written to a git-ignored file.

**No retries** (CLAUDE.md 7.5): a journey that is flaky counts as failing.
Tests run with one worker, because they share the seeded database.

**Where it runs:**

- `make e2e` runs locally, and `make verify` now includes it;
- the CI job `e2e` runs after `build` and `frontend`, with two steps in the
  §7.4 order: journeys, then accessibility. Each step reseeds the database.
  The job uploads `e2e-results/` (traces and failure screenshots).

## What the gate found, and what changed

- **Denials that cannot be decided.** The seed routes denials on claims that
  are partly paid (56 of 104) or already replaced. Every decision refuses a
  claim that is not `Denied`, so the workbench offered forms that could only
  fail after the operator had filled them in. Now the Decide panel says why
  the denial cannot be decided here and offers nothing else
  (`undecidable()`, `NotDecidable` in `DenialWorkbenchScreen`). The journeys
  pick their denials the same way the screen judges them
  (`decidableDenials`).
- **A blank Decide panel after a write-off.** The write-off confirmation lived
  in the request form. When the denial reloaded, the denial was resolved and
  the panel rendered nothing. Now an applied write-off shows its amount, the
  reason, who asked for it and who approved it (`Applied` in
  `WriteOffPanel`).
- **Journeys assert the settled screen, not a transient message.** A success
  message that a reload unmounts is not what an operator is left looking at.
  Journeys 4 and 5 check what stays on screen. Journey 5 then checks the
  replacement claim on the claim page.

## Not covered

- **Reflow (WCAG 1.4.10)** remains the documented exception in ADR-0015:
  UR-33 fixes a 1366×768 desktop viewport.
- **Partly paid denials.** Deciding single lines of a partly paid claim is not
  built. The screen now says so rather than failing.
- **The queue does not mark undecidable denials before they are opened.**
  `QueueItemOut` does not carry the claim's state. Showing it in the queue is
  an API change, and it is tracked as follow-up work.

## Consequences

- Tests:
  - `frontend/e2e/journeys.e2e.ts`: 5 journeys;
  - `frontend/e2e/accessibility.e2e.ts`: 14 scans;
  - `DenialWorkbenchScreen.test.tsx`: 3 new tests (cannot be decided,
    applied write-off, decided outcome still shown).
- **Measurements:** recorded in `docs/M5_GATE_EVIDENCE.md`.
- **Rollback:** remove the `e2e` CI job and the `make e2e` target. The
  stage guard will then fail CI until the stage is wired again, which is
  intended. The screen changes are presentation only; no migration or API
  change is involved.
