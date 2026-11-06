# ADR-0016: Denial dispositions — write-off approval and the patient's share

**Status:** Accepted · **Date:** 2026-11-06 · **Milestone:** M5 (M5.7 slice 1)

Amends the DOC-02 §7 claim state machine. Supersedes no earlier ADR.

## Context

- **S-04 (SRS 10.4):** the Denial Workbench offers four dispositions —
  correct & resubmit, appeal, patient responsibility, write off.
- **DOC-02 §7 state machine:** a `Denied` claim may go only to `Appealing`,
  `Correcting` or `WrittenOff`. There was no route to `PatientBalance`, so
  "patient responsibility" on a denial had nowhere to go. The two
  specifications disagreed.
- **BR-M11-05:** write-offs above `[TBD-48]` need named human approval.
  SRS 11.5 gives AR and Denial specialists a write-off limit of
  "≤ threshold", and the Ops Manager "Any".
- **PD-19:** a CO-group amount never appears on a patient's bill.
- `[TBD-48]` is undecided.

## Decision 1: `Denied → PatientBalance`, for the patient's share only

The owner chose (2026-11-06) to add the transition. It is guarded by
`patient_share_denied`: the denied lines must carry a PR-group amount. The
amount billed is that PR share and nothing else. Moving the claim accepts the
payer's refusal; the CO amount is the provider's contractual loss and is never
billed to the patient.

A denial with no PR amount is refused with a reason ("contractual amounts are
never billed to the patient"), and the claim stays `Denied`.
`test_co_group_never_billed_to_patient` fails if the amount ever includes a
non-PR group (shown).

DOC-02 §7's diagram gains `Denied --> PatientBalance: PR share only`.

### While a CO amount is open, the patient is not billed (interim)

A denial can carry both, for example CO-50 $5,000 beside PR-1 $10. Billing the
patient would move the claim out of `Denied`, and the $5,000 would be dropped
with no write-off and no approval (BR-M11-05). So `bill_patient` refuses while
any non-PR amount remains on the denied lines, and says to appeal, correct or
write that off first. The screen explains this instead of offering the button.

**Real-world practice (researched 2026-11-06), and the target:**

- The group code decides who can be billed. CO is the provider's (the patient
  must not be billed); PR may be billed.
- Billing teams treat the two parts separately: the PR part goes to the
  patient as the 835 states it, and the denied CO part stays open in denial
  work until it is appealed, corrected or written off. It is never pushed onto
  the patient.
- A denial write-off is a discretionary adjustment under the organisation's
  own approval levels, not a contractual one.
- Medicare: only PR is billable to the beneficiary, and a non-covered service
  only with a valid ABN.

The target is that split: bill the PR share at once and keep the CO part as
open denial work. It needs a patient balance kept apart from the claim's
state, which belongs with patient statements (not built yet). The interim rule
is one function (`bill_patient`), with nothing stored that depends on it, so
it can change after testers' review. The Medicare ABN check belongs in the
same later change.

## Decision 2: a write-off covers the payer's share, never the patient's

The amount written off is the denied lines' adjustments outside the PR group.
Forgiving a patient's cost share is a different, regulated act, and a denial
write-off must not do it by accident.

## Decision 3: approval above `[TBD-48]`, by someone else

- A specialist's write-off above `[TBD-48]` waits. The approver needs the new
  `write_off:approve` permission (granted to the Ops Manager) and may not be
  the requester.
- An Ops Manager's write-off applies at once: their limit is "Any".
- `[TBD-48]` is the placeholder setting `write_off_approval_threshold` at
  **0.00**. Until the owner sets the figure, every specialist write-off waits.
  That is the fail-closed reading of an open decision, not a chosen policy.

## Decision 4: the record cannot be rewritten

`write_off` (the request) and `write_off_decision` (at most one per request)
are insert-only by grant (migration 0025). Status is read from the two, never
stored as a mutable flag. Because Postgres needs the UPDATE grant for
`SELECT … FOR UPDATE`, decisions serialise on the denial's work-item row
instead. The request, the decision, the claim transition and the work item's
resolution happen in one transaction, each audited. Every disposition needs
an `Idempotency-Key` (PD-14).

## Consequences

- Closing a `WrittenOff` or `PatientBalance` claim is the next action, due
  the same day. Nothing automates it yet.
- Only fully `Denied` claims are disposed of here. A `PartiallyPaid` claim
  with denied lines needs a separate decision.
- Write-offs waiting for approval are visible on the denial page. A queue of
  them for approvers is not built yet.
- A disposition acts only on an open denial item. A resolved item is history:
  the claim may since have been denied again under another payment.
- The operator's free-text note stays on the work item's resolution, never in
  the claim's state history (PD-13).
- "Not your own approval" compares subjects case- and space-insensitively.
  A write-off that goes to wait releases the requester's hold on the item, so
  the approver is not blocked by it.
- `audit_event.action` was widened from 64 to 255 characters (migration 0026),
  because `POST /api/v1/denials/<id>/disposition` could not be audited and was
  refused.
