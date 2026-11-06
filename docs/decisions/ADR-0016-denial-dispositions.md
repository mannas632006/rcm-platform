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
- `audit_event.action` was widened from 64 to 255 characters (migration 0026),
  because `POST /api/v1/denials/<id>/disposition` could not be audited and was
  refused.
