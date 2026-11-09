# ADR-0024: Promote a rule from the Rules screen

**Status:** Accepted · **Date:** 2026-11-10 · **Milestone:** M5.9

## Context

- M5.9 asks for: *"Rule Builder with backtest results. Promote disabled
  until backtest passes; disabled state states why."*
- UR-30: *"The Promote control is disabled until a backtest has completed and
  its false-positive rate is below the configured threshold. The disabled
  state must state why."*
- The lifecycle has existed since M4.5. `lifecycle.change_status` is the only
  way a rule's status changes, and it refuses each step by name:
  - into SHADOW without a recorded backtest;
  - into BLOCKING without a blocking severity;
  - into BLOCKING with any client's false-positive rate unmeasured or above
    the cap (FR-RULES-03, 10%);
  - into a live status while another version of the rule is live.
- Until now the Rules screen was read-only. Rules moved only through
  `tools/m4_gate.py` and the feedback loop.

## Decision

**One set of checks, asked two ways.**

- The checks in `change_status` move into `lifecycle._check`, which writes
  nothing.
- `lifecycle.promotion(row)` asks `_check` about the version's next forward
  step (`PROMOTION`: draft → backtesting → shadow → advisory → blocking) and
  returns the step and the refusal reason, if there is one.
- `change_status` asks the same `_check` before it writes. The screen cannot
  offer a step the API refuses, and cannot hide one the API would take.

**What the screen reads:** `GET /api/v1/rules` now carries:

- `next_status` and `promotion_blocked_by` for each rule;
- `fp_rate_cap` for the list as a whole.

Promotion is judged on every client's backtests, as the step itself is: a
rule that is switched on is switched on for all clients. The reason can
therefore name a rate measured on a client outside the caller's scope. It is
a rate, not PHI, and it is what the step would refuse with anyway.

**The step:** `POST /api/v1/rules/{rule_id}/activate` (the SRS §7 route).

- **Permission:** `rule:promote`, which only the rules author holds (M4.5
  keeps it separate from `rule:author`).
- **Body:** `{version, to_status, reason}`. The step is named, so a second
  press from a stale screen is refused with a 409 rather than taken as the
  step after it.
- **Locking:** the row is locked from the check to the write.
- **Retries:** an Idempotency-Key is required, so a retry promotes once
  (PD-14).
- **Record:** the existing `rule_status_change` row records who asked, why,
  and the backtest the step rested on.

**The screen** (`RulesScreen`):

- A Promote column. A rules author sees "Promote to Shadow" and so on.
  - When the step would be refused, the button is disabled and the
    lifecycle's reason appears beside it as visible text, tied to the button
    with `aria-describedby`. It is not a tooltip (UR-30).
  - Other roles see "Next: Shadow" and no control.
- Pressing Promote opens a dialog that asks why. Promoting to Blocking warns
  first that a blocking rule stops every matching claim, for every client.
- A false-positive rate above the cap is shown in amber, with an icon and the
  words "above the 10.00% cap".
  - The comparison is exact, on scaled integers, so 10.004% is above the cap
    and 9.996% is not.
  - Amber, not red: an advisory rule does not put money at risk (UR-31).
- If the list arrives without a cap, nothing is marked (PD-18).

## Not in scope

- Authoring, editing and backtesting a rule from the screen: the condition
  builder, scope, action and "run backtest" in S-06. Rules are still authored
  as versioned documents (ADR-0007) and backtested by the harness (M4.4).
- UR-17's sample of the claims a rule would have blocked. It needs the
  backtest's flagged claims stored per run, which is an M4.4 schema change.
- Withdrawing a rule (to DRAFT) and deprecating one. Both are lifecycle steps
  but not promotions, and they remain tools-only.

## Consequences

- Tests:
  - `tests/integration/test_rule_promotion_api.py`: 6 tests, including the
    invariants `test_the_rules_list_says_why_promotion_is_blocked` and
    `test_a_rule_above_the_cap_shows_why_and_cannot_be_made_blocking`;
  - `RulesScreen.test.tsx`: 6 new tests;
  - an axe scan of the screen as a rules author, with the dialog open.
- The M4 lifecycle tests are unchanged and pass: `_check` is the same code,
  moved.
- **Rollback:** remove the route and the column. The two new fields in the
  list can stay, because the client ignores them. No migration is involved.
