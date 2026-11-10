# ADR-0026: Model training, tracking and the model registry

**Status:** Accepted · **Date:** 2026-11-10 · **Milestone:** M6.1, M6.2, M6.5

## Context

- M6.1: *"MLflow, training pipeline, model registry. Data hash + code commit
  recorded per run."*
- M6.2: *"ML-1 denial risk (LightGBM). Must beat the rules-only baseline or
  be rejected."*
- M6.5: *"Confidence thresholds as versioned config. Hard-coded threshold
  fails CI."*
- SRS 9.2:
  - FR-ML-01: the leakage suite runs before every training job.
  - FR-ML-02: splits are temporal, 18 / 3 / 3 months.
  - FR-ML-03: every model version records its training data hash, code
    commit, hyperparameters and full evaluation results.
  - The diagram: reject a model that does not beat current production *and*
    the rules baseline.
- M2 left the pieces training needs:
  - `mart_final_outcome`: the rows whose outcome has matured (DR-09);
  - `features.MART_COLUMNS`: what each mart column is (feature, label, key,
    provenance);
  - `training.split()`: temporal only, every other strategy refused by name.
- CLAUDE.md section 8: synthetic data *"must NOT be used to train any
  production model"*.

## Decision

### Where it lives

- `rcm.analytics.dataset` reads the training rows from the mart. Analytics
  owns the warehouse; nothing above it reads mart tables.
- `rcm.decision.ml` trains, evaluates, registers and (later) serves. It sits
  in the decision layer beside the rules engine it is measured against.

### The training rows (ML-1)

- One row per service line, from `mart_final_outcome`. The mart's grain is
  one row per line per adjustment (PD-6); every adjustment row of a line
  carries the same features and the same `is_denied`, so the line is taken
  once. The grain of the mart is unchanged.
- Lines never adjudicated (`is_denied` null) are left out. They are not
  clean claims, and reading them as such teaches the model that rejected
  claims are fine (PD-4).
- **The leakage gate runs first (FR-ML-01).** `features.assert_trainable`
  refuses, by name, any column that is not a FEATURE, and any feature still
  listed in `PENDING_COLUMNS` (always null: it would train on nothing). It
  runs before a row is read. The empirical as-of rebuild in `tests/leakage`
  blocks every merge (NFR-TEST-01), so code that trains has passed it.
- **No CPT descriptor text.** BR-GLOBAL-03 gates training on CPT
  *descriptor text* behind the AMA licensing decision (`[TBD-36]`). The
  features carry procedure *codes* only; the mart has no descriptor column,
  and `assert_trainable` refuses any column whose name says it is one.
- **The application role reads the marts, and only the marts.** dbt grants
  `rcm_app` SELECT on them (`+grants`, with USAGE on the schema in
  `on-run-end`). Serving (M6.4) will score a claim from the same mart row it
  was trained on, so training and serving cannot see different features.

### The split (FR-ML-02)

- Train on the first 18 months of submissions, validate on the next 3, test
  on the 3 after. The boundaries are dates; no row is shuffled.
- Validation picks the number of trees (early stopping). Test is read once,
  for the report and the accept or reject decision.
- A window with no rows on either side refuses to train
  (`TemporalSplit.is_usable`).

### The model

- LightGBM, binary, with `deterministic=True`, a fixed seed and one thread.
  The same rows and the same code produce the same model, byte for byte;
  that is tested. PD-10 asks that every layer be rebuildable from RAW and
  code, and a model is one.
- Categorical features are encoded with a vocabulary frozen on the training
  window. A value first seen later is *missing*, not a new code. The
  vocabulary is stored with the model.

### The rules baseline (M6.2)

- The published ruleset, with the NCCI and payer-registry checks, is run
  over each test-window claim exactly as the scrubber would (`evaluate`),
  writing nothing. A line counts as flagged when its claim would be blocked
  or advised.
- Both are scored on the same test lines by average precision (PR-AUC). The
  model is accepted only if its PR-AUC is above the baseline's, and above
  the current ACTIVE version's when there is one. Anything else is
  registered as REJECTED with the numbers that rejected it.
- The report also carries ROC-AUC, the Brier score, calibration by decile
  (expected calibration error) and PR-AUC per payer.

### Tracking and the registry (FR-ML-03)

- **MLflow** records each run: hyperparameters, every metric, and as tags
  the data hash, the code commit and the data source. The tracking store is
  a local SQLite file (`RCM_ML_TRACKING_URI`); `mlflow-skinny` is enough.
  MLflow is a training-time dependency only (the `ml` extra); the API never
  imports it.
- **`ml_model_version`** (migration 0028) is the platform's registry: what
  a running system may load. Each row holds:
  - the model name (`ML-1`) and version;
  - the verdict: `ACCEPTED` (it may enter shadow) or `REJECTED`, with the
    reason and its numbers; shadow and active come with M6.6, in their own
    table;
  - the data hash: SHA-256 over the training rows, in a canonical order;
  - the code commit, and whether the tree was clean;
  - the data source, `SYNTHETIC` or `REAL`;
  - hyperparameters, metrics, baseline metrics, the thresholds version, the
    split windows and the MLflow run id;
  - the model itself, as LightGBM's text format, and its encoding.
- **Insert-only by grant.** A trained model's record is evidence; it is
  never rewritten. Status changes (M6.6) are new rows in their own table.
- **A model trained on synthetic data cannot run in production.** The
  registry refuses to load a `SYNTHETIC` version when
  `RCM_ENVIRONMENT=production`.
- **The data source is read from the data, not passed in.** Every claim and
  remittance keeps the raw file it came from (PD-9), and every X12
  interchange says what it is: ISA15 is `P` for production and `T` for test.
  The generator writes `T`. `provenance.data_source` reads the envelope of
  every 837 and 835 behind the training rows: one test interchange makes the
  version SYNTHETIC. Nobody can register a synthetic model as REAL by typing
  the wrong flag, because there is no flag. This is how the pipeline meets
  `tools/synthetic`'s demand that M6 refuse its corpus: it is never refused
  for *training*, which M6 needs, and always refused for *production*,
  which is what CLAUDE.md section 8 forbids.

### Thresholds (M6.5, FR-ML-07, BR-HITL-03)

- `rcm/decision/ml/thresholds.toml` holds every number a model decision
  turns on, with a `version`. It ships inside the package, so the API reads
  the same file it was reviewed as, and TOML needs no parser beyond the
  standard library. Every prediction records the version it was judged by.
  Changing a threshold is a reviewed change to that file, with the version
  raised; the loader refuses a file whose version is missing.
- **No hard-coded threshold.** A test parses `rcm.decision.ml` and fails on
  any comparison against a numeric literal other than 0 or 1. **Negative
  shown:** with `if probability >= 0.7:` added, it fails naming the file and
  line.
- **None of the values is decided.** No `[TBD-nn]` covers them. Each is a
  labelled placeholder, `decided: false` in the file:

  | Value | Placeholder | Why this default |
  |---|---|---|
  | ML-1 review threshold: P(denial) at which a claim goes to a person before it is sent | 0.50 | The point of equal odds; carries no claim about this book of business |
  | Minimum lift over the baseline's PR-AUC to accept | 0.00 | The spec says "beats"; any larger margin is a sponsor's call |
  | Drift: PSI warning / critical (M6.7) | 0.10 / 0.25 | The conventional bands for PSI |
  | Calibration: ECE critical (M6.7) | 0.10 | |
  | Coverage: minimum share of predictions served (M6.7) | 0.90 | |
  | Shadow period before promotion (M6.6) | 28 days | SRS 9.2 says "N weeks" |

## Consequences

- New runtime dependencies: `lightgbm` and `numpy`, and the runtime image
  gains `libgomp1` (OpenMP), which LightGBM needs. The `ml` extra adds
  `scikit-learn`, `mlflow-skinny` and `shap`; `dev` includes it, so every
  CI job has it. None had a known vulnerability on 2026-11-10 (pip-audit).
- `tools/train_model.py` trains, evaluates, tracks and registers, and prints
  the report. Locally:
  - `make ml-corpus` generates the full 24-month corpus into `data/full`;
  - `make ml-db` loads it into `rcm_ml` (`infra/postgres/03`), builds the
    rules system on it with `tools/m4_gate.py` and builds the warehouse;
  - `make ml-train` trains.

  Training never touches `rcm` (the operator UI's) or `rcm_test` (wiped by
  the suites).
- In CI the leakage job, which already builds the warehouse, runs the whole
  pipeline on the 6-month smoke corpus with 3 / 1 / 1 windows
  (`tests/leakage/test_ml_pipeline.py`).
- **Rollback:** drop migration 0028's table and the package. Nothing else
  reads either yet.
