# ADR-0031: The owner's decisions of 2026-11-12

**Status:** Accepted · **Date:** 2026-11-12 · **Decided by:** Muhammad Anas · **Completed by:** ADR-0035 (the four open settings, decided 2026-11-13)

## Context

- CLAUDE.md section 12: a `[TBD-nn]` value nobody has decided is a
  configurable setting with a labelled placeholder default, reported until
  someone decides it. It is never a guess passed off as a decision.
- `rcm.platform.placeholders` held nineteen such settings. The application
  logged each at startup.
- `thresholds.toml` (ADR-0026, ADR-0028) held every model threshold as a
  placeholder, `decided = false`, at version 3.
- ADR-0029 left one question open: whether the synthetic generator should
  simulate appeals so that ML-5 can be built.

On 2026-11-12 these were put to the owner, with the value each setting was
running on.

## Decision

### Operating values: adopted as they stood

| Setting | ID | Decided value |
|---|---|---|
| `timely_filing_escalation_days` | TBD-37 | 14 days |
| `payer_registry_staleness_days` | TBD-42 | 180 days |
| `preventable_denial_defect_threshold` | TBD-44 | 3 recurrences |
| `bulk_handling_threshold` | TBD-46 | 25.00 |
| `write_off_approval_threshold` | TBD-48 | 0.00 |
| `outcome_maturity_days` | TBD-50 | 180 days |
| `raw_file_retention_days` | TBD-55 | 2557 days (7 years) |
| `session_idle_timeout_minutes` | TBD-56 | 15 minutes |
| `work_item_lock_expiry_minutes` | TBD-57 | 30 minutes |
| `appeal_follow_up_days` | none (ADR-0017) | 30 days |
| `submission_ack_followup_days` | none (M3.7) | 2 days |
| `outbox_retry_seconds` | none (M3.7) | 300 seconds |
| `outbox_max_attempts` | none (M3.7) | 12 attempts |
| `clearinghouse_breaker_failures` | none (M3.7) | 5 failures |
| `clearinghouse_breaker_open_seconds` | none (M3.7) | 60 seconds |

- **TBD-48 at 0.00 is a choice, not an omission.** Every specialist
  write-off waits for a named approver who is not the requester
  (BR-M11-05). An Operations Manager's limit stays "Any" (SRS 11.5).
- TBD-45 (the 10% false-positive cap) was decided on 2026-11-03 and is
  unchanged.

### Model thresholds: adopted as they stood, as version 4

| Threshold | Decided value |
|---|---|
| ML-1 review at P(denial) | 0.50 |
| Lift a candidate must exceed to be accepted (PR-AUC, macro-F1) | 0.0 |
| Drift warning, PSI | 0.10 |
| Drift critical, PSI (reverts the model) | 0.25 |
| Calibration error, critical | 0.10 |
| Minimum coverage | 0.90 |
| Drift window | 30 days |
| Predictions below which nothing is judged | 200 |
| Shadow period before promotion | 28 days |

- `thresholds.toml` is `decided = true`, version 4. The values are the
  same as version 3's, so a prediction judged under either was judged by
  the same numbers; the version says which file judged it.
- None of these was tuned to a book of business. 0.5 is equal odds and
  0.10 / 0.25 are the conventional PSI bands. Tuning them against real
  outcomes is a later decision and a later version.

### ML-5 stays deferred

The generator will not simulate appeals. A model trained on invented appeal
outcomes learns only the invention (CLAUDE.md section 8). ADR-0029 stands;
ML-5 is built when real appeal outcomes exist.

## What is still open

- **Four settings no specification gives**, still reported at startup:
  `next_action_days`, `exception_sla_hours`, `exception_trend_ratio`,
  `exception_trend_min_count`.
- **TBD-36**, the AMA CPT licensing position: a legal determination, not a
  number. No model trains on CPT descriptor text until it is made
  (BR-GLOBAL-03).
- **DECISION-01 to DECISION-08** (SRS section 16). Three are settled by what
  was built: DECISION-02 (a modular monolith), DECISION-04 (X12 parsing is
  built, not bought) and DECISION-07 (analytics stays in PostgreSQL). The
  other five are business choices that gate production, not this build:
  offshore PHI access (01), the error-tracking vendor (03), cloud provider
  and region (05), the secondary clearinghouse (06), and whether coding is
  built or bought (08).

## How a decision is held

- `placeholders.DECIDED` records each decided setting with who decided it
  and when. A decided setting leaves `REGISTRY`, so the startup log reports
  only what is still open.
- `tests/unit/test_placeholders.py` holds each decided setting's default to
  the decision. Changing a decided value fails that test until the decision
  itself is changed.
- A threshold change is a reviewed change to `thresholds.toml` that raises
  `version` (FR-ML-07, BR-HITL-03), as before.

## Consequences

- No behaviour changes: every decided value is the value the platform was
  already running on.
- `docs/M6_GATE_EVIDENCE.md` was measured under thresholds version 3. Its
  numbers stand; version 4 holds the same values.
- **Rollback:** revert the commit. The settings return to placeholders and
  are reported again.
