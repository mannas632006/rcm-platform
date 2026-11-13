# ADR-0033: Answers to claim status enquiries, and who looks next

**Status:** Accepted · **Date:** 2026-11-13 · **Requirement:** DOC-02 M10, EC-54, EC-56 · **Supersedes:** ADR-0030's deferral of EC-54 and EC-56

## Context

- DOC-02's AR follow-up module asks the payer where a claim is (a 276) and
  acts on the answer (a 277): finalised and paid but no 835, chase the
  remittance; denied, route to denials; not on file, resubmit; pended for
  information, a person with the reason; no answer, portal then a call.
- EC-54: *"Payer 277 says paid but no 835 or funds received."* EC-56:
  *"Payer repeatedly pends without stating a reason."* Both were deferred
  (ADR-0030) because a 277 answering an enquiry was quarantined as an
  unknown category.
- A 277 status response (implementation guide 005010X212) and a 277CA
  (005010X214) share every segment. Their categories do not mean the same
  thing: a 277CA says accepted or rejected; a status response says paid,
  pending, finalised, not found.
- The owner chose, on 2026-11-12, to build this before a real clearinghouse
  connection exists.

## Decision

### A status answer is read as an answer

- `parse_277` marks a transaction under 005010X212 as a status response.
  There each STC01-1 category (code list 507) maps to a `StatusAnswer`:
  PAID, DENIED, NO_PAYMENT, FINALIZED, PENDING, INFO_REQUESTED, NOT_FOUND,
  RETURNED, NO_ANSWER, ACKNOWLEDGED. STC05, the amount the payer says it
  paid, is read.
- A category outside the list still quarantines the file (PD-18). A 277CA
  is read as before: a status-response category on a 277CA is unknown, and
  quarantines.

### It is kept apart, and it moves nothing

- Answers land in `claim_status_answer` (migration 0031, insert-only by
  grant), never in `claim_acknowledgement`. Claim progression reads only
  277CA verdicts and 835s, so an answer cannot move a claim.
- "Denied" on a 277 is not a denial: the denial is what the 835 says, with
  its reasons (PD-4). The answer says the 835 should exist.

### `rcm.work.status_follow_up` decides who looks next

It reads each Submitted or InAdjudication claim with no live 835 posting
and looks at its latest answers:

| Latest answer | Follow-up | Item |
|---|---|---|
| PAID, DENIED, NO_PAYMENT, FINALIZED | after `status_remittance_grace_days` with no 835: ask for the remittance | EC-54 |
| PENDING, the last `status_pending_repeat_limit` in a row, none stating a reason | call the payer and ask what it waits for | EC-56 |
| PENDING with a reason | wait | |
| INFO_REQUESTED | send what the payer asks for | |
| NOT_FOUND | confirm receipt; resend if it never arrived | |
| RETURNED | correct and resend (a rejection, PD-4) | |
| NO_ANSWER | portal, then a call | |
| ACKNOWLEDGED | wait | |

- **A pend states no reason** when its STC12 is empty and its status code
  is empty, 0 ("cannot provide further status electronically") or 20
  ("accepted for processing").
- Each follow-up is one `CLAIM_STATUS_FOLLOW_UP` item in AR Follow-up,
  owned by the AR specialist, and one decision record (PD-1). It is raised
  once while open.
- An item names the claim and the payer's codes. The payer's free text
  never goes in it, because it can carry a patient's details (PD-13); it is
  kept on the answer.
- A claim under appeal is not read (EC-57). Only the client's own answers
  and claims are read (PD-20).
- `tools/follow_up_status.py` runs it twice; the second run must raise
  nothing.

### Two new placeholders

Neither number is given by any specification. Both are labelled and
reported at startup until the owner decides them:

| Setting | Provisional |
|---|---|
| `status_remittance_grace_days`: days after a "decided" answer before a missing 835 is chased | 14 |
| `status_pending_repeat_limit`: reasonless pends in a row before a person asks why | 3 |

## Limits

- **The platform does not send a 276.** Sending one is a clearinghouse
  transaction. The mock adapter sends 837s only. Answers are read when a
  277 arrives, however it was asked for. The scheduled status sweep (SRS
  Stage 4, tasks 4.3 and 4.4) is not built.
- **Funds are not seen.** EC-54's "or funds" needs deposit data (EC-48,
  still deferred). The item asks a person to check the deposit.
- **A later answer leaves an earlier item open.** If a claim pended without
  reason is then answered "paid", the pending item stays until a person
  closes it, and the new answer raises its own.

## Consequences

- EC-54 and EC-56 move from DEFERRED to COVERED in `tests/EDGE_CASES.md`,
  with the limits above in their rows.
- **Rollback:** revert the commit. Migration 0031's downgrade refuses once
  an answer or a follow-up item exists, since those are records.
