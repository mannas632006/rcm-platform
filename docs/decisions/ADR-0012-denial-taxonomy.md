# ADR-0012: What a denial is, and which of ten categories it falls in

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M4 (M4.8)

## Context

- **DOC-02 glossary:** a denial is a "payer refusal to pay a line or claim,
  with a reason code".
- **BR-M11-01 / BR-M11-02:** every denial is classified by root cause and
  given a preventability determination.
- **BR-TAX-01:** every denial maps to exactly one of the ten categories of
  DOC-02 §8.1, and the CARC→category mapping is version-controlled.
- **The open question.** Until now, a line counted as denied when it "paid
  nothing and gave a reason". That rule lived in three places:
  `rcm.analytics.denials`, `core_trailing_denial_rates` and
  `mart_claim_line_outcome`. Claim progression held 135 claims it would not
  call (ADR-0006). Each had a zero-paid line whose only reasons were in
  group PR.

## Decision 1: a denial is a refusal, read by its reason, not its group

A service line is **denied** when the payer paid nothing on it and gave at
least one reason that is neither a patient cost share nor a payment
adjustment.

- **Patient cost share** is exactly PR-1 (deductible), PR-2 (coinsurance)
  and PR-3 (copay). The payer approved the service and the patient owes it.
  That is not a refusal.
- **Payment adjustments** are CARC 45 (fee schedule), 23 (a prior payer
  already paid) and 24 (capitation), whatever the group. The payer priced
  the service; it did not refuse it. Real 835s send CO-45 beside PR-1 on a
  line the deductible swallowed whole, so without this, every January's
  deductible claims would read as denials. A secondary paying nothing after
  the primary (OA-23) is the correct outcome. Found in review: the synthetic
  generator caps patient share at 30% and never sends 23 or 24, so the
  corpus could not show it.
- **PR-204** ("not covered under the current benefit plan") **is** a
  refusal. Group PR only says who may be billed for it.
- **A reversed posting's denial still happened.** The payer reverses a
  claim to take a payment back, and the generator reverses whole claims, so
  a denied line rides along with the paid one. The denial rate, the
  classifier and the mart all count it; the mart's `final_outcome` says
  REVERSED. The reversal posting itself (every sign flipped) is never read
  as a refusal (BR-M9-04). Review suggested excluding reversed originals;
  doing so dropped one real denial from the corpus rate (104 → 103), which
  `test_denial_rate_excludes_resubmissions_at_corpus_volume` caught.

The rule is defined once, as `COST_SHARE` and `PAYMENT_ADJUSTMENT` in
`rcm.analytics.denials`. It is applied in that module's query, in both dbt
models, and in claim progression. All four change together, and a test
fails if the dbt spelling drifts.

Measured effect on the synthetic corpus:

- **Denial rate:** unchanged. The generator never pays a line to zero with
  cost share alone.
- **Progression:** the 135 held claims were all PR-204 refusals. They are
  denials, and progression now treats them so.
- **Still held:** a line paid nothing with no refusal (cost share or a
  payment adjustment took it all) goes to a person, as before.

## Decision 2: the taxonomy is versioned data, in one place

`rcm.decision.denials` holds the ten DOC-02 categories, each one's
preventability and the module that could have prevented it. It also holds
the CARC→category table, under `TAXONOMY_VERSION = "taxonomy-v1"`. The
mart's `denial_category` and `preventability` read the same table through
`core_denial_taxonomy`, and a test fails if the two drift apart.

Mapping choices worth stating:

- **CARC 16** ("claim lacks information") → Registration/Eligibility. EC-59:
  it is often sent with no RARC to say what is missing, so the category is
  the best the CARC allows. A person refines it.
- **CARC 252** (documentation required) → Medical Necessity. The
  documentation is what supports necessity. Review flagged it as arguably
  administrative and fully preventable, but DOC-02 has no Documentation
  category, and BR-TAX-01 fixes the list at ten. Payers send 252 when they
  want records before deciding the service was warranted, which is
  Medical Necessity's partial preventability. Kept (owner: "do what is
  best", 2026-11-03).
- **CARC 59** (multiple or concurrent procedure rules) → Coding. On a line
  paid nothing, it is a multiple-procedure edit the scrubber can catch
  (M5/M7). Kept for the same reason.
- **Payer Error** has no CARC. A person assigns it, when an appeal shows the
  payer was wrong.

## Decision 3: one category per denied line

When a line has several refusal reasons (EC-62), the reason with the largest
amount decides. A tie goes to the lowest CAS sequence, then the adjustment
id: CAS sequence is not unique on a line, and the mart orders the same way.
A payment adjustment never decides, so a larger CO-45 cannot hide a
preventable CO-50 from the feedback loop. Every reason considered is
recorded, so the choice can be checked.

A refusal CARC missing from the table is never guessed into a category,
even when a smaller refusal on the line is mapped: the category comes from
the reason that cost the most, or from a person (PD-18). The mart labels
such a line `UNMAPPED`. `rcm.work.denials.classify_and_route` writes a
`CLASSIFY_DENIAL` decision record saying it was held, and opens one named
DATA_QUALITY exception per line, keyed by taxonomy version. Reviewing and
extending the table is the quarterly review BR-TAX-01 asks for.

## Decision 4: stored once, recorded as a decision

`denial_classification` holds one row per denied 835 line per taxonomy
version, insert-only by grant (migration 0021). A composite foreign key ties
each row to its line's client and posting, so it cannot sit under another
client (PD-20). Classifying again under the same version reads nothing; a
concurrent run that loses the race skips the line (`ON CONFLICT DO
NOTHING`) rather than fail. Each classified claim payment writes one
`CLASSIFY_DENIAL` `decision_record` listing its lines, and any unmapped
lines beside them (PD-1).

## Rollback

`alembic downgrade 0020` while nothing is classified. Once rows exist the
downgrade refuses: decision records cite them, and the table is
insert-only. Reverting `COST_SHARE`, `PAYMENT_ADJUSTMENT` and the three
queries together restores the earlier denial rule.
