# ADR-0019: Scrubber and payment exception queues get their own named producers

**Status:** Accepted · **Date:** 2026-11-08 · **Milestone:** M5 (M5.8)

Builds on ADR-0006 (claim progression), ADR-0007 (rules engine) and ADR-0011
(auto-correction). Supersedes nothing.

## Context

- **M5.8:** "Scrubber exception and payment exception queues: named
  findings, not generic failures." **BR-EXC-01:** every exception names the
  specific problem and the specific record.
- Both queues existed on screen (`work/views.py`), but nothing raised
  `SCRUB_EXCEPTION` or `PAYMENT_EXCEPTION` items, so both were always empty.
- The scrubber ran only inside correct & resubmit (ADR-0018) and the M4 gate
  replay. Claims arrive as 837s the practice management system has already
  sent (ADR-0006), so no claim reaches the scrubber before it is sent.
- Progression already held claims on three 835 posting problems (several
  postings, no service lines, a line paid nothing with no refusal reason).
  It raised them as `DATA_QUALITY` items, which go to the ops correction
  owner rather than the payment poster.
- The owner asked (2026-11-06) for real-world choices to be made without
  waiting, and recorded so testers can revisit them.

## Decision 1: scrub sent claims in review mode, and raise each blocking finding

`rcm.work.scrub_review.review_client` scrubs every claim that is `Submitted`
or `InAdjudication`: sent, and not yet decided by the payer. A team can
still correct and replace such a claim before the 835 arrives. Real teams
call this claim watch or post-submission edit review.

- **Review, never correct.** `scrub_claim(..., apply_corrections=False)`
  evaluates once and records the decision (PD-1, `"mode": "review"`), but
  writes no fix. A fix applied to a sent claim would make the stored row
  disagree with the 837 the payer holds (PD-9, PD-10).
- **One item per blocking finding,** titled
  `Scrubber rule <id> v<version> (<reference>) blocks line <n>: <message>`
  and routed where the rule's `route_to` says (scrubber, eligibility or
  auth queue). Advisory and shadow findings are recorded, never raised.
- If the PHI check refuses the rule's own wording (PD-13), the item is still
  raised, named by rule and line. The full message stays in the decision
  record.
- **Once per claim per ruleset:** a claim reviewed under the latest ruleset
  is skipped. A newly published ruleset reviews the waiting claims again,
  and an unchanged finding keeps its one open item.
- **No ruleset is an error,** never a clean result (PD-18).
- Run with `python -m tools.review_scrubs`, after `tools.advance_claims`.

## Decision 2: 835 posting problems go to the payment queue

`Problem` now carries the kind it raises. "Several 835 postings", "835 with
no service lines" and "835 patient-share-only line" raise `PAYMENT_EXCEPTION`
(owner: payment poster). Problems about acknowledgements and voids stay
`DATA_QUALITY`. Titles and source keys are unchanged.

## Decision 3: Claim Detail lists what is open on the claim

`ClaimDetailOut.open_problems` lists the open work items on every submission
of the claim, soonest SLA first: kind, named title, detail, owner, assignee,
due date. S-03 shows them under "Open problems", so a person who opens a
claim sees its scrubber finding or payment exception without searching the
queues.

## Open, for testers' review

- **Items left over from before.** Open `DATA_QUALITY` items for the three
  posting problems, raised before this change, stay in the data-quality
  queue. The next progression run raises the matching `PAYMENT_EXCEPTION`
  item beside each one, so the same problem shows in both queues until the
  old item is closed. Close the old items (outcome `NO_ACTION_NEEDED`, note
  "moved to the payment queue") once testers confirm the routing.
- **Findings on claims the payer has since decided.** A scrubber item stays
  open after the 835 arrives. Closing it automatically would be an
  automatic resolution (BR-EXC-05). Today a person resolves it, with
  `NO_ACTION_NEEDED` or after correcting the claim.
- **No suspension switch.** The review only records decisions and raises
  items, and never changes a claim, so it has no `AutomationPath` of its
  own. Adding one needs a migration to the suspension CHECK.
- **More payment exceptions.** An 835 for a claim not on file, overpayments
  and PLB-only takebacks are real payment-posting exceptions. They are not
  produced yet.

## Consequences

- The scrubber queue fills only when a published ruleset has a blocking
  rule that fires on a sent claim. On the synthetic corpus that is the M4
  rules.
- Rollback: stop running `tools.review_scrubs`, and set the three `Problem`
  kinds back to `DATA_QUALITY`. No migration.
