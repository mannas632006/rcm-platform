# ADR-0009: How a rule backtest counts

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M4 (M4.4)

## Context

BR-M7-04: every rule is tested against historical claims before
deployment, with its false-positive rate measured. FR-RULES-03 / TBD-45: a
rule above the threshold may not block. The owner set TBD-45 to 10% on
2026-11-03. M4.4 asks for evaluated / flagged / TP / FP / FP-rate.

The SRS does not say what a true positive is, or what to do with claims
that have no outcome yet.

## Decision

A backtest runs one rule version over one client's claims, through
`evaluate`, as a shadow. A draft can be backtested; nothing about any claim
is decided.

Each claim is scored on its own outcome:

| Rule said | Claim was | Counted as |
|---|---|---|
| flagged | denied | true positive |
| flagged | paid | false positive |
| passed | denied | missed denial |
| passed | paid | (correct, not counted apart) |
| INDETERMINATE | either | indeterminate |
| anything | no 835 yet | without outcome |

- **Denied** uses `rcm.analytics.denials`, the definition behind the denial
  rate: a line paid nothing with a reason, on a posting that is not a
  reversal. A backtest and the denial rate count the same thing.
- **Outcome known** means a posting that is not a reversal is on file. A
  claim with none is never counted as paid.
- **FP rate** = false positives / flagged, to four places. When nothing is
  flagged it is null, not 0%: a rule never seen stopping a claim has shown
  nothing about its accuracy, so it cannot pass a threshold.

Every run is kept in `rule_backtest`, insert-only by grant (migration
0019), with its rule version, client and counts. M4.5 promotes on these
records.

## Limits, for later

- Truth is per claim. A rule flagging a claim denied for an unrelated
  reason scores a true positive. Matching the denial reason (CARC) to the
  rule needs M4.8's denial taxonomy.
- One client per run (PD-20). Promotion across clients is M4.5's call.
- Line-level truth needs a match from 835 service lines to 837 lines, which
  the loaders do not yet make.

## Rollback

`alembic downgrade 0018` while no backtest is recorded.
