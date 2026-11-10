# M5 gate evidence — operator UI

CLAUDE.md section 5, GATE M5: *"Playwright E2E covering the five
highest-volume journeys. Axe accessibility scan passes WCAG 2.1 AA. Report
both."*

How the gate is built, and why each journey was chosen, is in ADR-0023. This
file has the measurements.

---

## How it was measured

```
make up            # Postgres + Redis
make seed-smoke    # the smoke corpus the seed loads
make e2e           # rcm_test to head, then journeys, then axe
```

- Playwright 1.64.1, Chromium, at the supported 1366×768 desktop viewport
  (UR-33). One worker, **no retries** (CLAUDE.md 7.5).
- Before each run, `tools/e2e_seed.py` wipes `rcm_test` and rebuilds it the
  way the system fills it:
  - it loads the smoke corpus (600 claims) and advances the claims;
  - it classifies and routes denials, publishes a ruleset and reviews scrubs.
- The queues the journeys work are the ones that produces: 616 claim-step
  reviews, 104 denials and 1 scrubber exception.
- The API (port 8010) and the UI (port 5183) are started by Playwright, so
  the journeys never touch a developer's running stack.
- axe-core 4.14 runs with tags `wcag2a`, `wcag2aa`, `wcag21a` and `wcag21aa`.

## Result

Two consecutive full runs on 2026-11-10, each **19 passed, 0 failed,
0 flaky, 0 skipped**. The second run:

### Journeys (E2E)

| # | Journey | Result | Time |
|---|---|---|---|
| 1 | Sign in and pick up work from My Work | pass | 1.1 s |
| 2 | Work an AR follow-up item by keyboard: take it, resolve it, say why | pass | 2.5 s |
| 3 | Look a claim up, read it, and jump to its decision audit | pass | 1.3 s |
| 4 | Write off a denial as a deliberate second step, then go to the next | pass | 2.9 s |
| 5 | Correct a denied claim and resubmit it, after previewing every change | pass | 3.4 s |

Each journey drives the real UI by keyboard where SRS 10.9 gives a key.

- **Journey 4:** the write-off is not made until the confirmation button is
  pressed (UR-37).
- **Journey 5:**
  - resubmitting is disabled until the change has been previewed (UR-10);
  - the member ID is masked to its last four characters in the diff (S-6);
  - the replacement claim is then found in the original's submission chain,
    not as a new claim (PD-7).

### Accessibility (axe, WCAG 2.1 AA)

| Screen or state | Violations | Time |
|---|---|---|
| Sign in | 0 | 1.1 s |
| My Work | 0 | 1.7 s |
| Queue: AR Follow-up | 0 | 2.3 s |
| Queue: Denial Workbench | 0 | 2.2 s |
| Queue: Scrubber Exceptions | 0 | 1.4 s |
| Search results | 0 | 1.3 s |
| Claim Detail | 0 | 2.1 s |
| Dashboard | 0 | 1.4 s |
| Rules | 0 | 2.0 s |
| Payer Registry | 0 | 2.1 s |
| Not found | 0 | 1.3 s |
| Keyboard shortcuts dialog (open) | 0 | 1.5 s |
| Denial Workbench, decision panels, then write-off open | 0 | 3.0 s |
| Resolve dialog (open) | 0 | 1.5 s |

**Total: 14 scans, 0 violations.** The one documented exception is reflow
(WCAG 1.4.10, ADR-0015). UR-33 fixes a desktop viewport, and axe does not
test reflow.

## What the gate found

The first run had 17 passing and 2 failing. The two failures pointed at
product behaviour, not only at the tests:

1. **Undecidable denials.**
   - 56 of the 104 routed denials are on partly paid claims, and some are on
     claims already replaced.
   - Every decision refuses a claim that is not `Denied`, so the workbench
     offered forms that could only fail after they were filled in.
   - Fixed: the Decide panel now says why the denial cannot be decided here,
     and offers nothing else.
2. **The Decide panel went blank after a write-off was applied.**
   - The confirmation was rendered inside the request form, and the reload
     unmounted the form.
   - Fixed: an applied write-off now shows its amount, the reason, who asked
     for it and who approved it.

The journeys now assert what stays on the screen, not a message that flashes
and is gone.

**After #23 merged, CI failed journey 5 once** (run 42765275907):

3. **A correction refused without saying why.**
   - Denials that tie in the queue have no fixed order, so journey 5 took a
     different denial in CI: CLM00000530, denied CO-197 (no authorisation).
   - Changing only the member ID leaves the authorisation missing. The
     scrubber refused the replacement, which is correct (PD-18).
   - The panel showed only "failed domain validation". The server had sent
     the reason, in the problem's `context`, and the client dropped it.
   - Fixed:
     - The error panel now shows the server's reason: here, that the payer
       requires prior authorisation for this procedure.
     - An action that fails says so ("Could not send the corrected claim"),
       not "Could not load".
     - Journey 5 asks for the denials a new member ID answers: CO-27 or
       CO-31. Replayed through the API, all 13 such decidable denials in
       the seed resubmit; CLM00000530 is refused, with that reason.
4. **The first test in a file could fail on a slow machine.** A lazy-loaded
   screen took longer to compile than `findBy`'s one second. The Vitest
   setup now loads the screens before any test waits.

The run after these fixes, locally on 2026-11-10: **20 passed, 0 failed**
(5 journeys; 15 axe scans, 0 violations).

## M5 tasks

| Task | Status | Where |
|---|---|---|
| M5.1 Shell, routing, auth, generated client | done | PR #15; `api:check` in CI fails on hand edits |
| M5.2 Design tokens | done | `check-tokens.mjs` in `npm run lint` |
| M5.3 Virtualised `WorkQueueTable` | done | 10,000 rows keep a few dozen in the DOM (`QueueScreen.test.tsx`) |
| M5.4 Keyboard model | done | ADR-0021; journeys 2–5 |
| M5.5 Claim Detail + Decision Audit | done | PR #16; journey 3 |
| M5.6 `DenialCodeChip` | done | `DenialCodeChip.test.tsx` |
| M5.7 Denial Workbench, diff preview, appeal | done | PRs #17–#19; journeys 4–5 |
| M5.8 Scrubber and payment exception queues | done | PR #20, ADR-0019 |
| M5.9 Rule Builder with backtest results | done | ADR-0024. Promote is disabled until the lifecycle would take the step, with its reason in words beside it (UR-30); `test_rule_promotion_api.py`. Authoring and backtesting from the screen (S-06's condition builder) stay out of scope, as ADR-0024 records |
| M5.10 Payer Registry with staleness flags | done | `PayerRegistryScreen` |
| M5.11 Dashboards | done | KPIs and claims by state, segmentable by client and by payer (UR-19, ADR-0025) |
| M5.12 Partial-state handling | done | ADR-0020; `test_model_degradation_falls_back_to_rules` |

Every M5 task is now done. **After M5.11:** the GATE M5 run on 2026-11-10
had 20 passed and 0 failed, and the dashboard scan includes the segment
controls.

**After M5.9:** a rules-author scan was added: the Rules screen with its
Promote controls, then the promote dialog open. The run on 2026-11-10 had
**20 passed, 0 failed** (15 axe scans, 0 violations).

## Invariant tests for M5

| Directive | Test | Status |
|---|---|---|
| M5.7 / FR-DEN | `test_appeal_requires_human_signoff` | pass |
| M5.11 / UR-19, PD-20 | `test_a_client_outside_my_scope_cannot_be_segmented_to` | pass. **Negative shown:** without `require_client`, a caller scoped to client A got client B's figures (`200 != 403`) |
| M5.9 / UR-30 | `test_the_rules_list_says_why_promotion_is_blocked`, `test_a_rule_above_the_cap_shows_why_and_cannot_be_made_blocking` | pass |
| M5.12 / UR-34 | `test_model_degradation_falls_back_to_rules` (model absent; model failing) | pass. Added at this gate: M5.12 had tested the behaviour under other names. **Negative shown:** with the score key's direction reversed in `queue_order()`, both cases fail (`['CLM-A', 'CLM-B'] != ['CLM-B', 'CLM-A']`) |
