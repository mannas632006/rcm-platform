# ADR-0010: Rule lifecycle, and what each promotion rests on

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M4 (M4.5, M4.6)

## Context

- **FR-RULES-06:** promotion goes draft → backtest → shadow → advisory →
  blocking, "no shortcuts". SRS §8.3 draws the steps.
- **FR-RULES-03:** a rule above `[TBD-45]` may not be BLOCKING. The owner set
  TBD-45 to 10% on 2026-11-03.
- **M4.5:** promotion without a backtest must be impossible through the API.

## Decision 1: the steps are data

`lifecycle.TRANSITIONS` lists every allowed step, exactly as SRS §8.3 draws
them. Anything else is refused by name:

| From | To |
|---|---|
| DRAFT | BACKTESTING |
| BACKTESTING | DRAFT, SHADOW |
| SHADOW | ADVISORY |
| ADVISORY | BLOCKING, DRAFT |
| BLOCKING | DEPRECATED, DRAFT |
| DEPRECATED | — |

`change_status` is the only code that moves a rule's status. It locks the
rule version and records each step in `rule_status_change`, insert-only by
grant (migration 0020). Each record names who made the change and why.

## Decision 2: what each step rests on

| Into | Requires |
|---|---|
| SHADOW | a recorded backtest of this exact rule version that saw at least one claim with an outcome |
| BLOCKING | the rule's severity is `BLOCKING`, and every client's latest backtest of the version has a measured FP rate, with the worst at or below the cap |
| any live status | no other version of the rule is live |

- **The cap.** It is `Settings.blocking_rule_max_false_positive_rate`, a
  `Decimal` defaulting to `0.10`. TBD-45 is no longer listed among the open
  placeholders: the owner decided it.
- **The worst client decides.** A rule that does well for one client and
  badly for another would block the second client's good claims.
- **A null FP rate never passes the cap.** A null rate means the rule
  flagged nothing (ADR-0009).
- **The database backs the record up.** A CHECK refuses a SHADOW or BLOCKING
  record that names no backtest.

## Consequences

- **Not yet built.** SRS §8.3 asks that "shadow results confirm" before
  ADVISORY, and that the "operator override rate [is] low" before BLOCKING.
  Neither can be measured until findings are worked by people (M5). Until
  then, the step needs a stated reason, and BLOCKING also needs the backtest
  above.
- **Status still has one other writer.** The application role keeps
  `UPDATE (status)` on `rule_version` (migration 0017). Integration tests use
  it for setup. The API will expose only `change_status`.
- **Rollback.** `alembic downgrade 0019` while no status change is recorded.
