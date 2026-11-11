# ADR-0027: The models that decide at the denial: ML-2 and ML-3

**Status:** Accepted · **Date:** 2026-11-10 · **Milestone:** M6.3

## Context

- M6.3: *"ML-2 denial category, ML-3 recovery, ML-5 appeal success."*
  ML-5 has its own ADR, because it needs data the corpus does not yet
  have (appeals and their outcomes).
- SRS 9.1:
  - ML-2: multiclass; input *"line + CARC + RARC"*; output *"taxonomy
    category"*.
  - ML-3: binary; input *"denial + history"*; output *P(recovery)*.
- ADR-0026 built ML-1, which decides the second before SEND (PD-11). Its
  leakage gate let a model read FEATURE columns only. ML-2 and ML-3 decide
  later, once the 835 has said no, and they cannot work without reading
  the 835.
- The category is already decided, without a model, by a fixed table:
  `CARC_CATEGORY` (BR-TAX-01, ADR-0012), mirrored by `core_denial_taxonomy`.
  The mart's `denial_category` is that table's answer for the line.
- The owner's decision (2026-11-10, option a): build ML-2 as specified,
  with the taxonomy table as the rules baseline it must beat, and let the
  gate reject it if it does not.

## Decision

### The leakage gate knows when a model decides

- `features.Stage`:
  - `SUBMISSION` (ML-1): only FEATURE columns.
  - `DENIAL` (ML-2, ML-3): the FEATURE columns, plus the 835's own facts
    in `KNOWN_AT_DENIAL`: group code, CARC, RARCs, adjustment amount, claim
    status, paid and allowed, category and preventability, and days to
    adjudication.
- Nothing from after the 835 is ever an input at the denial. That includes
  `final_outcome`, `net_collected`, `rework_touches` and `is_denied` taken
  as a later fact. Each is refused by name, and tested.
- **A model never reads its own answer.** `assert_trainable(label=...)`
  refuses the label, and any column `DERIVED_FROM` it, at every stage.
  `preventability` is looked up from the category, so ML-2 may not read it.
- **Measured, not only argued.** `test_denial_facts_knowable_at_denial`
  takes every line already denied on the leakage suite's cutoff date. Its
  `KNOWN_AT_DENIAL` values must be identical in the full build. A later file
  (a resubmission's 835, a reversal, an appeal's reprocessing) that
  rewrote them would fail it. This holds because the warehouse keeps the
  first non-reversal posting as the line's adjudication
  (`core_claim_adjudication`), and later money shows up only in
  `net_collected`.

### One row per denied line

- Both readers take one row per service line from `mart_final_outcome`,
  the matured rows (DR-09), as ML-1's does.
- A line's row is its first refusal: a non-PR row before a patient-share
  (PR) row, then CAS order. The patient's share is not why the payer
  refused.

### ML-2: denial category

- **Rows:** matured denied lines the taxonomy classified. An UNMAPPED line
  has no answer to learn from, so it is left out rather than taught as a
  category of its own (PD-18 keeps it unclassified).
- **Inputs:** ML-1's line features, plus `carc_code`, `cas_group_code` and
  `rarc_codes` (the RARC list as one code).
- **Model:** LightGBM multiclass, deterministic as ML-1 is. The category
  names are stored with the encoding (`classes`), so a registered model
  answers in categories. Its classes come from training and validation; a
  category first seen in test is one it never answers, and is scored as a
  miss.
- **Measure:** macro-F1 over the categories present in the test window.
  Every category counts the same, so the model cannot win by being right
  only about the common ones. Accuracy and per-category F1 are reported.
- **Baseline:** the taxonomy table, applied to the line's CARC exactly as
  the platform classifies. A CARC it does not hold answers UNMAPPED, which
  is a miss.
- **Acceptance:** macro-F1 above the table's, and the current version's,
  by more than `acceptance.min_macro_f1_lift` (a placeholder, 0.0). A tie
  is not a win.
- **Expected result on this corpus:** REJECTED. The label *is* the table's
  answer, so the table scores at or near 1.0, and the most a model can do
  is tie it. That is the gate doing its job: the platform does not deploy
  a model to repeat a lookup it already makes for free. ML-2 earns a place
  only when the categories come from somewhere the table cannot see, such
  as a person reclassifying a denial (PAYER_ERROR has no CARC at all).
  That is data the platform does not record yet.

### ML-3: recovery

- **Rows:** matured denied lines.
- **Answer:** recovered, meaning a later submission in the same chain
  (PD-7: resubmissions chain to the original) was paid on the same
  procedure. A resubmission has the same date of service as its original,
  so it matures the same day, and a matured denial's answer is complete.
- **Inputs:** ML-1's line features, plus the denial's CARC, group code,
  category, preventability, adjustment amount and days to adjudication.
- **Baseline:** what the work queue believes of every denial today:
  `prioritisation.P_RECOVERY["DENIAL"]`, one constant for all of them. A
  constant ranks nothing, so its PR-AUC is the share recovered. It is the
  honest rival, because it is what ML-3 would replace (SRS 7.8).
- **Acceptance:** as ML-1. PR-AUC above the baseline's, and the current
  version's, by more than `acceptance.min_pr_auc_lift`.
- **On this corpus:** the generator resubmits a denial by a coin flip
  (`resubmission_rate`), whatever the denial says, and the correction it
  applies is what decides payment. ML-3 can learn which denials get paid
  once corrected. It cannot learn who chooses to resubmit, because nothing
  chooses. This is a property of the synthetic data (CLAUDE.md section 8),
  and the numbers mean no more than that.
- **The generator had to change for there to be an answer at all.** Its
  payer refused every resubmission as a CARC 18 duplicate of its own
  original, so no denial in the corpus was ever recovered and the label was
  constant. A corrected claim (REF*F8) replaces its original; the payer now
  calls a line a duplicate only when another claim chain already billed it.
  `test_corpus_contains_a_recovered_resubmission` holds the generator to it.

### Thresholds

- `thresholds.toml` goes to version 2 and gains
  `acceptance.min_macro_f1_lift`. Like every value in it, it is a labelled
  placeholder (`decided = false`).
- No review threshold is added for ML-2 or ML-3. Neither is served yet
  (M6.4), and a threshold nothing reads would be a number with no decision
  behind it.

### What did not change

- ML-1's rows, features, data hash and acceptance are as ADR-0026 left
  them. The pipeline is shared now, and ML-1's tests are unchanged.
- The registry table. ML-2's classes travel in the encoding JSON, which a
  binary model writes exactly as before. No migration.
- `Model.drivers` refuses a multiclass model. Which class's SHAP block to
  persist is M6.4's decision, not a default taken here.

## Consequences

- `make ml-train` trains ML-1, ML-2 and ML-3. `--model` picks a subset.
  Each model is its own transaction, so one that refuses to train leaves
  the ones before it registered.
- In CI, the leakage job trains all three on the smoke corpus (3 / 1 / 1),
  and checks each against its baseline and the registry.
- **Rollback:** revert the change. The registry rows it wrote are evidence
  and stay. Nothing serves these models yet.
