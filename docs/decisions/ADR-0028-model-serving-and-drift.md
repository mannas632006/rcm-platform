# ADR-0028: Serving models, shadow, drift and the auto-revert

**Status:** Accepted · **Date:** 2026-11-11 · **Milestone:** M6.4, M6.6–M6.9

## Context

- M6.4: *"SHAP drivers persisted per prediction. PD-1 satisfied for model
  decisions."*
- M6.6: *"Shadow deployment harness. Model output recorded, not used,
  comparison reported."*
- M6.7: *"Drift monitoring: PSI, calibration, coverage."*
- M6.8: *"Auto-revert to rules-only on critical drift. Test: simulate drift →
  model path disables itself unattended."*
- M6.9: *"Graceful degradation. Missing features → rules path, never a
  guessed prediction."*
- SRS 9.2–9.4:
  - FR-ML-04: no model reaches production without a shadow period.
  - FR-ML-05: every prediction persists a `decision_record` with model
    version, inputs, output, confidence and SHAP drivers.
  - FR-ML-06: missing features never produce a guessed prediction.
  - FR-ML-08: critical drift reverts to rules-only without waiting for a
    human.
- PD-5: deterministic rules always override model output on conflict.
- ADR-0020 built the queues' seam for P(recovery); ADR-0026 and ADR-0027
  built the registry and ML-1, ML-2 and ML-3.

## Decision

### Status in service is a table, not a flag

- `ml_deployment` (migration 0029) holds every change of a version's status:
  SHADOW, ACTIVE, REVERTED or RETIRED, with who and why. A version's status
  is its latest row. A process that restarts reads the same status every
  other process does, so a reverted model stays reverted.
- Only an ACCEPTED version may enter shadow. Only a version that has been in
  shadow for `shadow.min_days` (thresholds) may be promoted. Promoting one
  retires the ACTIVE one.
- `tools/ml_serve.py` is the operator's way in: `status`, `shadow`,
  `promote`, `revert` (each needs `--by` and `--reason`), `score` a window,
  `compare` a version, `monitor`.

### Scoring: one door

- `rcm.decision.ml.serving.score` is the only way a model is asked
  anything. Every serving version scores the lines; only the ACTIVE
  version's scores come back. A SHADOW version is recorded and returns
  nothing (FR-ML-04).
- **The inputs are the mart's.** Serving reads `mart_claim_line_outcome`,
  of which training's `mart_final_outcome` is the matured part, through the
  same leakage gate (PD-11). Training and serving cannot see different
  features.
- **Every score is a decision (FR-ML-05, PD-1).** One `decision_record` per
  line per version, with decision type PREDICT and `decided_by`
  `model:ML-1`. It holds:
  - the model version (logic version) and the thresholds version;
  - every input feature;
  - the score (as confidence and output);
  - the top five SHAP drivers, from LightGBM's own TreeSHAP;
  - whether it was used (ACTIVE) or only recorded (SHADOW);
  - for ML-1, the route the review threshold gives.
- `ml_prediction` indexes them: one row per line per version, with the
  score or the reason there is none. A line is scored once per version, so
  reading a queue twice does not make two decisions of one.
- Insert-only by grant, all three tables. Query-layer scoping holds: a line
  of another client is never scored, whatever ids are asked for (PD-20).
- A line the payer never adjudicated has no service line id (it comes from
  the 835) and is not scored.

### Graceful degradation (M6.9, FR-ML-06)

- `OPTIONAL_FEATURES` names the features a line may lawfully lack: a
  modifier or extra diagnosis the claim does not carry, a trailing rate with
  nothing yet in its window, and the columns no source fills. A model has
  learned what their absence means.
- Any other feature is on every training row (a test holds the list to the
  data). A line without one is one the model never saw the like of. It is
  recorded as DEGRADED, with the missing names, and gets no score. Rules or a
  person decide that line.
- A claim the warehouse holds no row for yet is DEGRADED once, with that
  reason.
- A claim's ML-3 score is its denied lines' scores weighed by charge. A
  claim with any line unscored has no score: half an answer is not shown as a
  whole one.

### The live paths

- **The queues (ML-3).** The API installs `ServedRecovery` behind the
  ADR-0020 seam at startup. Until a version is ACTIVE, the queue says why
  there are no scores ("in shadow", "withdrawn") and runs on the rules-based
  priority, as it did.
- **The scrub (ML-1, PD-5).** Before a claim is sent, the scrub asks ML-1
  for the claim's P(denial), which is its riskiest line's. The rules'
  verdict stands. The model can only raise PASS to ADVISE, when its score
  reaches `models.ML-1.review_at`. It never lowers a verdict and never
  blocks. The scrub's decision record holds the rules' verdict, the model's
  answer and the combined verdict. Any model failure is caught at that one
  boundary and the scrub goes on with the rules alone.
- **Today the live scrub degrades.** A claim being scrubbed before it is
  sent is not in the warehouse yet, so ML-1 records it DEGRADED and the
  rules' verdict stands. Scoring at submission needs the point-in-time
  features computed for a claim not yet in the mart. That is a separate
  piece of work; until it exists the scrub is safe and says why it had no
  score.

### Drift monitoring (M6.7) and the auto-revert (M6.8)

`monitoring.run` measures one serving version over the last
`drift.window_days` of its predictions:

| Check | How | Limit (thresholds) |
|---|---|---|
| Input drift | PSI of every feature: numbers on the reference deciles, codes on the commonest reference codes plus "other", and "missing" as a bin of its own | warning `psi_warning`, critical `psi_critical` |
| Output drift | PSI of the scores against the model's scores on its reference rows | the same |
| Calibration | Expected calibration error on predictions whose outcome has matured; the Brier score is reported | critical `ece_critical` |
| Coverage | Scored ÷ requested | critical below `min_coverage` |

- **The reference is the validation and test windows**, the latest rows the
  model was judged on and found good. It is not the training window. Its
  first months have trailing-rate windows still filling, so every later
  month looked drifted: on the full corpus, ML-1 on Oct–Nov 2025 against its
  training window showed payer × provider window size PSI 0.57, while its
  calibration error was 0.011.
- **The calendar is not drift.** `submission_month` and `submission_dow`
  shift by design: a later window is a later month. Against the training
  window `submission_month` scored PSI 6.4. The model still reads them;
  drift does not judge them.
- **Below `drift.min_predictions` nothing is judged.** The run says so. A
  model withdrawn on a handful of rows teaches people to ignore the alarm.
- **A CRITICAL breach reverts the version** in the transaction that records
  the run: a REVERTED row in `ml_deployment` by `monitor:auto`, with every
  critical breach in the reason. From the next request, `serving.score`
  answers "withdrawn" and scores nothing. Nobody is asked first (FR-ML-08).
- **Celery beat runs it daily** (`rcm.ml.monitor`, 06:00 UTC). Each run is a
  row in `ml_monitor_run`.

Measured on the full corpus, in rolled-back transactions:

| Window | ML-1 | ML-3 |
|---|---|---|
| Oct–Nov 2025 (inside the reference) | OK; ECE 0.011, output PSI 0.002 | WARNING: one input at PSI 0.14 |
| Jan–Mar 2026 (unseen; late claims only) | CRITICAL: `days_dos_to_submission` PSI 3.06, payer × provider rate 0.42; ECE still 0.020 | too few predictions (149) to judge |

The Jan–Mar 2026 revert is the monitor working. The corpus's encounters end
in December 2025, so the claims submitted in 2026 are late ones only, a
population the model was not built for. Its calibration held, and a person
may well decide to promote it again. That decision belongs to a person
reading the run, not to the model.

### The shadow comparison (M6.6)

`shadow.compare` reads a version's recorded scores against the outcomes that
have since matured. It reports them beside the baseline the version was
accepted against and, line for line, the ACTIVE version's scores on the same
lines. `tools/ml_serve.py compare` prints it.

### Thresholds (version 3)

Two values are added, both labelled placeholders like the rest
(`decided = false`):

| Value | Placeholder | Why this default |
|---|---|---|
| `drift.window_days`: days of predictions the monitor reads | 30 | SRS 9.4 says "daily monitor" and gives no window |
| `drift.min_predictions`: below it, nothing is judged | 200 | Enough for ten bins to mean something |

## Consequences

- GATE M6 is measured by `make m6-gate` on `rcm_ml`, in a transaction that
  is rolled back. Evidence: `docs/M6_GATE_EVIDENCE.md`.
- The API imports LightGBM at startup (it already did for the registry).
  scikit-learn and MLflow stay training-only (`ml` extra). `shadow.py`
  imports scikit-learn through `evaluate` and is used only by tools.
- **Rollback:**
  - Revert one model with `tools/ml_serve.py revert`. Every path falls back
    to rules: the queues show the reason; the scrub keeps the rules' verdict.
  - Remove the code by dropping `ServedRecovery` from startup and the scrub's
    call. Migration 0029's tables are expand-only, and the downgrade refuses
    once they hold records.
