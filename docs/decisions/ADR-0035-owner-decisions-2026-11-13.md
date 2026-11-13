# ADR-0035: The owner's decisions of 2026-11-13

**Status:** Accepted · **Date:** 2026-11-13 · **Decided by:** Muhammad Anas · **Completes:** ADR-0031's "What is still open"

## Context

- ADR-0031 left four settings open that no specification gives:
  `next_action_days`, `exception_sla_hours`, `exception_trend_ratio` and
  `exception_trend_min_count`.
- ADR-0033 raised two more for the 277 status follow-up:
  `status_remittance_grace_days` and `status_pending_repeat_limit`.
- On 2026-11-13 the six were put to the owner, with the value each was
  running on and an alternative either side of it.

## Decision

| Setting | Decided value | Provisional |
|---|---|---|
| `next_action_days`, before submission (`*`) | 1 day | 1 |
| `next_action_days`, Submitted | 2 days | 2 |
| `next_action_days`, InAdjudication | **21 days** | 30 |
| `next_action_days`, Paid, PartiallyPaid, Denied | **5 days** | 7 |
| `exception_sla_hours` | Compliance 4, external system 8, deadline risk 24, model low confidence 48, data quality 72, operational 72, rule conflict 120 | same |
| `exception_trend_ratio` | 1.5 | 1.5 |
| `exception_trend_min_count` | 10 | 10 |
| `status_remittance_grace_days` | 14 days | 14 |
| `status_pending_repeat_limit` | 3 answers | 3 |

- **The follow-up cadence is tighter than it ran.** A claim with the payer
  is chased after 21 days rather than 30, and a decided claim gets its next
  action after 5 days rather than 7. The owner chose sooner follow-up over
  fewer work items.
- The states the question did not name keep the values they ran on:
  Rejected 2, Correcting 3, Appealing 30, SecondaryBilling 3,
  PatientBalance 30, Reopened 2.
- Every other value is adopted as it stood.

## Consequences

- `placeholders.REGISTRY` is empty: the application logs no unresolved
  value at startup. The mechanism stays, for the next value a specification
  leaves open, and `tests/unit/test_placeholders.py` still proves it.
- **A behaviour change.** A claim moved into InAdjudication, Paid,
  PartiallyPaid or Denied from now on gets its next action due sooner.
  Claims already resting keep the due date they were given; nothing is
  rewritten (PD-1). Expect more AR follow-up items, earlier.
- ADR-0006's progression rules are unchanged: the window is a setting.
- **Rollback:** revert the commit. The six settings return to their
  provisional values and are reported as open again. A new value without a
  revert is another decision, recorded the same way.

## What is still open

- **TBD-36**, the AMA CPT licensing position, and **TBD-51 to TBD-54**
  (volume, concurrency, the real-file parser validation corpus).
- **DECISION-01, 03, 05, 06 and 08** (SRS section 16).
