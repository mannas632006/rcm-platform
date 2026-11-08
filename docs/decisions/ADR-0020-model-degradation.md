# ADR-0020: Model scores come through one seam that degrades, never blocks

**Status:** Accepted · **Date:** 2026-11-09 · **Milestone:** M5 (M5.12)

Supersedes nothing. M6 builds the model behind this seam.

## Context

- **UR-34 / SRS 10.8 PARTIAL:** where a screen combines core data with model
  output, model degradation must never stop an operator working a claim.
  Show the claim, and mark the score unavailable.
- **FR-ML-06:** missing features never produce a guessed prediction.
- **M5.12 acceptance:** model service down, and the claim is still workable.
- ML-3 (P(recovery)) arrives in M6. Before this change, the queue's
  `recovery_probability` was simply always null, with no reason given and
  no tested path for a model that fails.

## Decision

`rcm.decision.scoring.recovery_scores` is the only way the queues read
P(recovery):

- **No model deployed** (today): scores are unavailable, with the reason
  "No recovery model is deployed yet (M6)."
- **The model fails:** it raises, times out, breaks its own database work,
  or answers with something that is not a mapping. The failure is caught at
  this one boundary and logged by exception type only, never by message,
  which may echo inputs (PD-13). Scores are unavailable with the reason "The
  recovery model did not answer." The model runs in a savepoint, so its
  failure cannot poison the read around it.
- **An answer that is not a probability** (outside [0, 1], NaN, a bool, a
  string) is dropped for that claim. It is never clamped and never shown as
  0.
- The queue page carries `recovery_unavailable` (the reason, or null). The
  screen shows "P(rec) unavailable: <reason> The queue is ordered by the
  rules-based priority score, and every item can still be worked." Each
  missing cell reads "unavailable" to screen readers.
- Queue order never depends on the model. It uses the rules-based priority
  score (UR-02, PD-15), so a down model costs one column, never the order.

Catching every exception here is deliberate and limited to the model
boundary. The failure is logged and shown on screen, so it is not silent.
Elsewhere, named errors still propagate.

## Consequences

- M6 deploys ML-3 with `deploy_recovery_model(...)`. The degraded path is
  already tested: `tests/integration/test_queue_partial.py`.
- Other screens show no model output yet. Claim Detail and the Denial
  Workbench gain scores in M6 through the same seam and the same rule.
- Rollback: remove the seam, and the field reads null as before. No
  migration.
