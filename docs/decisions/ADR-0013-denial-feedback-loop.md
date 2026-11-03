# ADR-0013: The denial feedback loop drafts rules, and never promotes them

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M4 (M4.9)

## Context

- **SRS 8.4:** "This loop is the product." A denial is classified, judged
  preventable or not, checked against the rules that exist, and either
  exposes a failed rule or drafts a candidate that is backtested before it
  goes anywhere.
- **FR-RULES-07 / BR-M7-01:** every preventable denial produces a candidate
  rule or a recorded decision not to create one. An unexplained preventable
  denial recurring more than `[TBD-44]` times is a defect.
- **FR-RULES-06:** draft → backtest → shadow → advisory → blocking, no
  shortcuts. M4.5 enforces it; the loop must go through it like a person.

## Decision 1: every classified denial gets one answer, kept

`rcm.decision.feedback.run_feedback` answers each `denial_classification`
row not yet answered, under the same taxonomy version. The answer is one row
in `denial_feedback` (insert-only, migration 0023) and a `DENIAL_FEEDBACK`
`decision_record` per claim payment (PD-1):

| Outcome | When |
|---|---|
| `NOT_PREVENTABLE` | The category is not preventable (DOC-02 8.1). Logged, no rule. |
| `NO_RULE` | Preventable, but no claim field can catch it, with the reason. |
| `NO_CLAIM` | No 837 on file for the 835 claim. A person traces it. |
| `LINE_UNMATCHED` | The denied procedure is not on the claim, or is on several lines and the 835 line matches none by position. A person traces it. |
| `RULE_FAILED` | A rule acting on the line (ADVISORY or BLOCKING) flagged it at the claim's last scrub, for this cause, and it was denied anyway. |
| `ALREADY_COVERED` | A rule or NCCI edit would act on it now, for this cause; the denial predates it. |
| `CANDIDATE_DRAFTED` | A candidate was drafted for the pattern and backtested. |
| `CANDIDATE_EXISTS` | The pattern's candidate already exists; this denial joins it. |

Because every preventable denial is answered and the answer is kept,
BR-M7-01's "unexplained recurrence" cannot arise, and `[TBD-44]` has
nothing to measure. It stays unset.

"For this cause" means the rule declares the denial's category in
`provenance.category`. Every candidate does, and a hand-written rule may.
NCCI PTP counts as Coding (CARC 97) and MUE as Medical Necessity (CARC 151).
A rule that declares no category covers nothing: a coding flag on the line
says nothing about an authorisation denial, and matching on route alone
(review finding) would have read an NCCI MUE flag as a failed coding rule.

A shadow rule neither failed nor covers anything: it never acts on a claim.
A claim never scrubbed had no rule run on it, so none failed. "Covered now"
reads the claim as it stands, which a resubmission may have changed.

The denied 835 line is matched to the submitted line by position and
procedure, or to the only line with that procedure. Anything else is
`LINE_UNMATCHED`, never a guess.

## Decision 2: a candidate is broad, advisory, and a person's to promote

A pattern is one category, one payer, one procedure. The candidate:

- tests `line.procedure_code` under that payer and CPT scope, plus what the
  category allows. Authorisation adds `claim.has_prior_auth = false`.
- is `ADVISORY` at most, flags only, never auto-corrects, never adds a
  modifier (PD-16);
- carries `provenance.source = denial_feedback`, the category, CARC and
  taxonomy version;
- is moved to `BACKTESTING` and backtested on the client's history at once.
  A later denial from another client that joins it backtests it on that
  client's history too, so each client the rule could block has evidence.
- If the pattern's rule was retired (`DEPRECATED`) and the denial comes
  back, a new version is drafted, not joined to the dead one.

A broad rule will often show a high false-positive rate. That is the point
of the backtest: the number tells a person whether to sharpen it. The loop
never moves a rule past `BACKTESTING`; shadow, advisory and blocking stay a
person's steps, under M4.5's evidence gates.

| Category | Drafts | Layer | Routed to |
|---|---|---|---|
| Registration/Eligibility | yes | DEMOGRAPHIC | ELIGIBILITY_PROBLEM |
| Coordination of Benefits | yes | DEMOGRAPHIC | ELIGIBILITY_PROBLEM |
| Authorisation | yes | PAYER_SPECIFIC | AUTH_PROBLEM |
| Coding | yes | CLINICAL | SCRUB_EXCEPTION |
| Medical Necessity | yes | CLINICAL | SCRUB_EXCEPTION |
| Timely Filing | no: prevented by deadline priority (PD-15), not claim content | | |
| Duplicate | no: the scrubber reads one claim, not its history | | |
| Provider Enrolment | no: the scrubber has no provider field | | |

## Decision 3: what the loop cannot settle goes to a person

`rcm.work.denials.classify_and_route` runs classification, then the loop,
and raises, once per line per taxonomy version:

- each `RULE_FAILED` as a `RULE_CONFLICT` item for `rules_review`;
- each `NO_CLAIM` and `LINE_UNMATCHED` as a named `DATA_QUALITY` item: the
  loop could not learn from that denial, so it is not quietly answered.

## Decision 4: one run at a time

Candidates are keyed by payer and procedure, not client, so the loop takes
one transaction-scoped advisory lock for all clients. Two runs cannot draft
one pattern twice.

## Rollback

`alembic downgrade 0022` while `denial_feedback` is empty. Candidates
already drafted stay in `rule_version` as drafts that no ruleset holds.
