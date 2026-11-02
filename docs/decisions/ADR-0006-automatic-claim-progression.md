# ADR-0006: Claims move on their own; the submitter approves each step

**Status:** Accepted · **Date:** 2026-11-02 · **Milestone:** M3 (GATE M3)

## Context

GATE M3 asks for the full lifecycle end to end: created → scrubbed →
submitted → acknowledged → remitted → posted or denied, with no claim resting
in a non-terminal state without a next action date (BR-STATE-04).

Up to M3.7, every loaded claim sat in `Registered` with no next action date.
The state machine existed, but nothing moved claims through it.

Owner decision (2026-11-01): *"everything done automatically but visible to
the user who submitted the claim, so that he can review it and approve the
system's steps."*

## Decision 1: progression runs on evidence already on file

`rcm.work.progression.advance_all` moves each claim of one client as far as
its evidence goes, and no further:

| Evidence | Steps |
|---|---|
| An 837 we hold | `Registered` → … → `Submitted`. The auth branch is taken when the 837 carries a prior-auth number. |
| 277CA accepted | `Submitted` → `InAdjudication` |
| 277CA rejected | `Submitted` → `Rejected` (PD-4: never a denial) |
| 835, no 277CA | `Submitted` → `InAdjudication`, noted as *inferred from the 835* |
| 835 lines | All denied → `Denied`; some → `PartiallyPaid`; none → `Paid`. Uses the same denied-line rule as `rcm.analytics.denials`. |
| A later claim chained by REF*F8 | → `Replaced` (Decision 2) |

Evidence is matched on CLM01 alone, since `uq_claim_client_submitter_id`
allows one claim per CLM01 per client. It is never limited by load time,
because a 277CA or 835 can arrive before the 837 it answers.

A 999 moves nothing (BR-M8-02). Everything after an outcome (appeal,
correction, write-off, secondary or patient billing) is a person's decision
and stays one.

Each step writes a `decision_record` (`DecisionType.PROGRESS`, logic version
`progression-v1`) with the evidence counts and states. No PHI is recorded
(PD-1, PD-13). Every step goes through `claim_lifecycle.transition`, so the
row lock, the audit event and the next-action rule all still apply.

When the evidence contradicts itself (a 277CA rejection followed by an
835), no claim is moved. A `DATA_QUALITY` exception is raised instead
(PD-18).

A compliance suspension of `CLAIM_PROGRESSION` stops the whole run
(BR-EXC-03).

## Decision 2: a new final state, `Replaced`

DOC-02 §7.1 has no state for a submission that a corrected one supersedes.
BR-DATA-03 keeps one claim across its submissions, but each submission row
still needs an end, or the old row waits for a next action forever.

`Replaced` is final and resting. It can be reached from `Submitted`,
`Rejected`, `InAdjudication`, `Denied`, `PartiallyPaid`, `Paid` and
`Correcting`. The claim's follow-up continues on the replacing submission.

A void (frequency 8) is treated the same way for now. Telling a void apart
from a replacement is left to M4.

## Decision 3: every system step waits for review

- `claim_state_transition` gains a `review_status`:
  - `NOT_REQUIRED`: a person took the step, or the step predates review
  - `PENDING`
  - `APPROVED`
  - `SENT_BACK`

  Each reviewed step also records the reviewer, the time and a note. CHECK
  constraints require a reviewed step to name its reviewer, and a sent-back
  step to say why.
- Each claim with pending steps has one `CLAIM_STEP_REVIEW` work item, routed
  to `claim_submitter`.
- `approve_steps` approves every pending step and resolves the item. Steps
  taken later raise a new item.
- `send_back_step` marks one step and raises a `DATA_QUALITY` exception.
  Progression skips that claim until the exception is closed (PD-18).

Review does not hold the claim back while it waits: the claim keeps moving,
and review is how the submitter sees and disputes each step. That is the
owner's "done automatically but visible".

## Decision 4: a `sequence` column on `claim_state_transition`

`occurred_at` defaults to the transaction's start time, so steps taken in one
run share it, and history was ordered by a random UUID. `sequence` (an
identity column) fixes the order. History now orders by `occurred_at`, then
`sequence`.

## Next-action dates

BR-STATE-04 needs a date, and no specification gives one per state. They are
set in `Settings.next_action_days` and registered in
`rcm.platform.placeholders` as an Ops placeholder to confirm:

| States | Days |
|---|---|
| Pre-submission (default) | 1 |
| `Submitted`, `Rejected`, `Reopened` | 2 |
| `Correcting`, `SecondaryBilling` | 3 |
| `Paid`, `PartiallyPaid`, `Denied` | 7 |
| `InAdjudication`, `Appealing`, `PatientBalance` | 30 |

## Consequences

- Migration 0015 adds `Replaced`, the review columns, `sequence` and the
  `CLAIM_STEP_REVIEW` kind. The downgrade refuses while any `Replaced` claim
  or step review exists.
- A reversed payment is not progression's to act on. Reopening the claim
  (BR-STATE-03) stays a person's decision.
- Tests: `tests/integration/test_claim_progression.py` (GATE M3 path,
  idempotency, BR-STATE-04, review flow, suspension) and
  `tests/unit/test_claim_state.py` (`Replaced` edges).
- Gate measurements on the full corpus: `docs/M3_GATE_EVIDENCE.md`.
- A frequency-7 replacement that reuses its original's CLM01 is skipped by
  the claim loader as a resend. That is left to M4 (see the evidence file).

## Rollback

Stop calling `advance_all`. The claims it moved keep their recorded history.
To roll back the schema, run `alembic downgrade 0014` once no `Replaced`
claims or step reviews remain.
