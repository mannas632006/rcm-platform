# ADR-0005: Two state-machine edges that differ from the diagrams

**Status:** Accepted · **Date:** 2026-11-01 · **Milestone:** M3.1–M3.4

## Context

CLAUDE.md §9 requires an ADR when the build differs from the specification.
Two edges do, and until now they were explained only in code comments.

## Decision 1: `Assigned → Queued` on the work item machine

SRS §7.7 draws "released or timed out" only from `InProgress`. We also allow
it from `Assigned`.

**Why:** an operator who claims an item and leaves before opening it would
otherwise keep the lock forever, since no edge out of `Assigned` returns the
item to the queue. That is the failure FR-WORK-02 ("operators go home
mid-item") exists to prevent, one state earlier than the diagram anticipated.

**Where:** `rcm.work.state.TRANSITIONS`. Tested by
`test_an_expired_lease_returns_the_item_to_the_queue` and
`test_an_item_held_past_its_lease_can_be_taken_by_someone_else`.

## Decision 2: `PartiallyPaid → PatientBalance` is guarded by `no_secondary_coverage`

DOC-02 §7.1 labels `Paid → PatientBalance` with "no secondary coverage" but
leaves the `PartiallyPaid → PatientBalance` edge unlabelled.

**Why:** both edges leave the same states, `Paid` and `PartiallyPaid`, as
`SecondaryBilling`, which is guarded by `secondary_coverage_exists`. Without
the opposite guard, a partially paid claim with secondary coverage could go
straight to the patient. That would bill the patient for an amount the
secondary payer owes. The opposed pair makes the split exhaustive and
mutually exclusive, and a missing fact refuses both edges (PD-18).

**Where:** `rcm.domain.claim_state.TRANSITIONS`. Tested by
`test_opposed_guards_split_a_state_exhaustively` in
`tests/unit/test_claim_state.py`.

## Consequences

- The SRS §7.7 diagram and DOC-02 §7.1 should note both edges when they are
  next revised. This ADR is the record until then.
- Reverting either edge: drop it from the `TRANSITIONS` tuple. Neither edge has
  a schema footprint, so no migration is needed.
