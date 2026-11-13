# ADR-0034: Eligibility responses are stored, and the scrub asks about coverage

**Status:** Accepted · **Date:** 2026-11-13 · **Requirement:** DOC-02 M3, EC-13, EC-15, EC-61 · **Supersedes:** ADR-0030's deferral of EC-13, EC-15 and EC-61

## Context

- The 271 has been parsed since M1.6 and not stored. Its parser already
  kept the distinctions that matter: an active code is not the absence of
  an inactive one, a refused enquiry (AAA) is not a negative answer, and
  coverage is a period, not a state.
- DOC-02 M3 has no module in the M0-M6 plan. ADR-0030 deferred its six edge
  cases and EC-61 because no response was kept to hold a claim or a denial
  against.
- The owner asked, on 2026-11-12, for the eligibility module after the 277
  status answers.

## Decision

### The 271 is stored, without the subscriber's name or birth date

`eligibility_response` (migration 0032, insert-only) keeps, per 271
transaction: the payer, the member identifier, whether the payer answered
at all, every benefit (code, coverage level, service type, insurance type,
plan, coverage periods) and every AAA problem. The subscriber's name and
date of birth are not copied: no check needs them, and they stay in the
archived file (PD-9, PD-13).

### The scrub asks: covered on the date of service?

`rcm.decision.eligibility.EligibilityCheck` runs in Layer 5 (temporal)
beside the rules, the NCCI edits and the payer registry. It reads the
latest 271 from the claim's payer about the claim's member, received on or
before the scrub's date:

| The 271 says | Finding | Case |
|---|---|---|
| no coverage on the date of service | BLOCKING | EC-13 |
| only that it could not look (AAA) | ADVISORY: not a negative answer (PD-18) | |
| covered, with no period and no service detail | ADVISORY | EC-15 |
| covered on the date of service | none | |
| (no 271 on file) | none | |

- **No 271 on file is no finding.** Requiring an eligibility check before
  every claim is a practice's business rule, which the specifications do
  not set. The synthetic corpus carries no 271s, so the GATE M4 numbers are
  unchanged.
- **The member is not among the facts.** The facts a rule reads hold no PHI
  (ADR-0007), so the check is built for one claim. Its finding names the
  response by id, and the scrub's decision record keeps the response id,
  the day it arrived and whether it covered the date of service.
- **The latest answer decides.** A 271 that arrives later and says coverage
  ended before the date of service (a retroactive termination) blocks the
  next scrub; a scrub dated before it arrived still reads the earlier one.
- A replay through `shadow_verdict` (the ML-1 rules baseline) leaves the
  check out, because it has the facts and not the claim's member.

### The feedback loop knows when the payer contradicted itself

A coverage denial (CARC 26, 27 or 31) on a claim whose payer's latest 271,
received by the day the claim was sent, said covered on the date of service
drafts no rule. The loop records NO_RULE with that reason and the denial is
appealed (EC-61, FR-RULES-07). Logic version `feedback-v3`. A blocking
eligibility finding at the scrub that the payer then denied anyway counts as
a failed check, like the other built-in checks.

## Limits, and what stays deferred

- **The platform sends no 270.** Responses are read when a 271 arrives.
- **EC-14, service excluded:** needs a mapping from a procedure to the 271's
  service types. Not built.
- **EC-16, Medicare Advantage for Medicare:** needs the 271's plan identity
  read against the claim's payer. Not built.
- **EC-17, Medicaid as payer of last resort:** needs coordination of
  benefits across payers. Not built.
- Registration's own cases (EC-06 to EC-12) are untouched: there is still
  no patient record.

## Consequences

- EC-13, EC-15 and EC-61 move to COVERED. EC-14, EC-16 and EC-17 stay
  DEFERRED with the reasons above.
- **Rollback:** revert the commit. Migration 0032's downgrade refuses once
  any response is stored.
