# ADR-0032: ML-1 scores a claim before it is sent

**Status:** Accepted · **Date:** 2026-11-12 · **Milestone:** M6 (follows ADR-0028)

## Context

- ADR-0028 put ML-1 behind the scrub (PD-5) and said plainly what was
  missing: *"Today the live scrub degrades. A claim being scrubbed before it
  is sent is not in the warehouse yet, so ML-1 records it DEGRADED and the
  rules' verdict stands. Scoring at submission needs the point-in-time
  features computed for a claim not yet in the mart."*
- Two things stood in the way:
  1. **The inputs.** Serving read `mart_claim_line_outcome`, which dbt
     builds in batch. A claim loaded a moment ago has no row there.
  2. **The line's name.** A prediction was filed under `service_line_id`,
     which comes from the 835. Before a claim is sent there is no 835.
- PD-11: features must be knowable at submission. DR-07: trailing windows
  are computed as of the submission date. FR-ML-05: every prediction keeps
  its inputs, score and drivers. FR-ML-06: a missing feature is never
  guessed.

## Decision

### The inputs are computed from CORE, as the warehouse computes them

`rcm.analytics.submission.submission_lines` gives ML-1's inputs for any
claim in CORE:

- **Claim and line features** are written as `stg_claims`,
  `core_claim_submission` and `core_line_submission` write them. The as-of
  date is the same: the date the submitter stated, else the day the file
  was archived.
- **Trailing rates** are written as `core_trailing_denial_rates` writes
  them: adjudications that became known in the 90 days up to the day before
  the submission date, by payment date, across every client. The history is
  read from the mart, where the adjudications are. The claim being scored
  needs no row there, and its own outcome is never read: the window closes
  the day before it is sent.
- The leakage gate (`assert_trainable`, FR-ML-01) runs on the feature list
  here as it does for training and for serving from the mart.

**What holds the two descriptions together** is a test, not care:
`test_features_computed_at_submission_are_exactly_the_marts` computes every
ML-1 input for every submitted line the warehouse describes and requires
each to equal the mart's value. A change to a dbt model without the same
change here fails it.

Proved against the smoke corpus: shortening the window to 30 days, or
counting the submission day itself, fails it. Shortening it by one day
does not, because no adjudication in that corpus falls on day 90. The
full corpus would catch more; the test runs on the smoke corpus in CI.

### A prediction can name the submitted line

Migration 0030 (expand-only) adds `ml_prediction.claim_line_id`:

- A SCORED row names a line of one kind or the other.
- A score made at the scrub has `claim_line_id` and no `service_line_id`.
  Its decision record's entity is the `claim_line`.
- A score made from the warehouse keeps `service_line_id` and now carries
  `claim_line_id` too, so the two can be matched.

### A line is still scored once per model version

- `serving.score_submission` reads back any score the version already made
  for the line: at an earlier scrub, or from the warehouse.
- `serving.score` (the warehouse path) reads back a score made at the scrub
  for the same submitted line. It does not score the line again, and it
  returns that score under the 835's line.
- A unique index makes the first a constraint: one prediction per version
  per submitted line while it has no 835 line. Two scrubs racing for the
  same claim cannot both record; the loser's error is caught at the scrub's
  one model boundary and that scrub goes on with the rules alone.

### Monitoring and the shadow comparison find the outcome

A score made before the 835 is judged by the submitted line's outcome once
it matures. A line the payer answered twice, differently (a reversal), has
no single outcome and is left out of calibration.

### What the score does at the scrub

Unchanged from ADR-0028: the rules' verdict stands, and the model can only
raise PASS to ADVISE at `models.ML-1.review_at`. The scrub's decision record
holds the rules' verdict, the score and the combined verdict.

## Limits

- **The scrub records ADVISE; nothing stops for it yet.** The one live
  pre-submission path is correcting a denied claim and resending it
  (ADR-0018). That path refuses a replacement the rules BLOCK and sends
  anything else. A high ML-1 score is in the scrub's decision record, and
  so on the claim's Decision Audit panel, but the operator is not shown it
  before sending. Showing it in the correction preview is a change to the
  API and the screen, and is not in this ADR.
- **A trailing rate is as fresh as the last warehouse build.** An 835
  loaded since then is not in the history yet. The window errs towards
  knowing less, never towards knowing the future.
- **No warehouse, no score.** On a database where dbt has never run, the
  history query fails; the scrub catches it and uses the rules alone.
- **The models are synthetic.** A score says what the generator's patterns
  imply, not what a payer will do (CLAUDE.md section 8).

## Consequences

- A claim is scored when it is scrubbed, not after the next warehouse
  build. `docs/M6_GATE_EVIDENCE.md` no longer lists this as missing.
- GATE M6 is unchanged: it measures the warehouse path, which still scores
  each line once and reverts on drift.
- **Rollback:** revert the commit; the scrub goes back to the warehouse
  path. Migration 0030's downgrade refuses once any prediction names a
  submitted line, because those rows cannot be dropped.
