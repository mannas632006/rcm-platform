# M6 gate evidence — ML subsystem

CLAUDE.md section 5, GATE M6: *"Demonstrate auto-revert firing under
simulated drift, without human intervention."*

How the subsystem works is set out in ADR-0026 (training and registry),
ADR-0027 (ML-2 and ML-3), ADR-0028 (serving, shadow, drift and the
auto-revert) and ADR-0029 (ML-5, deferred). This file has the measurements.

**Every model here was trained on synthetic data.** It has learned only the
denial patterns the generator was told to produce (CLAUDE.md section 8). The
registry refuses to load it in production.

---

## How it was measured

```
make ml-corpus     # full 24-month corpus into data/full; empties rcm_ml first
make ml-db         # load it into rcm_ml, run the rules system (M4 gate), build the warehouse
make ml-train      # train ML-1, ML-2, ML-3; judge each; register them
make m6-gate       # the gate, in one transaction, rolled back
```

Corpus: 34,773 claims (773 resubmissions, 464 of them paid) in 12,169
files, 1 quarantined by design. Run on 2026-11-11.

## The models (M6.1–M6.3)

Test window Oct–Dec 2025; trained on the 18 months before it, validated on
the 3 after that.

| Model | Verdict | Model | Baseline |
|---|---|---|---|
| ML-1 denial risk | ACCEPTED | PR-AUC 0.7424 | rules, 0.2972 |
| ML-2 denial category | REJECTED | macro-F1 1.0000 | taxonomy table, 1.0000 |
| ML-3 recovery | ACCEPTED | PR-AUC 0.2335 | queue prior, 0.0526 |
| ML-5 appeal success | not built | | ADR-0029: no appeal outcomes exist |

ML-2 is rejected by design: the category is a fixed lookup from the CARC,
so the table is already perfect and a tie is not a win (ADR-0027).

## GATE M6: the auto-revert

`make m6-gate`, against `rcm_ml`, in a transaction rolled back at the end:

1. ML-1 v1 entered shadow on 2026-10-12, served the 28-day shadow period,
   and was promoted to ACTIVE on 2026-11-10.
2. **Control.** It scored the 13,527 lines it was validated and tested on.
   The daily monitor ran. **Severity OK, action NONE**: coverage 1.0,
   output PSI 0.0, calibration error 0.009. The alarm does not fire on the
   population the model was judged on.
3. **Simulated drift.** It scored 40,106 lines from the training months
   with every payer replaced by one it had never seen and every charge
   multiplied twenty-fold.
4. **The daily monitor ran as Celery beat runs it** (`monitoring.run_all`:
   no version named, nobody consulted):

   | Severity | Check | Value | Limit |
   |---|---|---|---|
   | CRITICAL | input: payer_id | 16.4037 | 0.25 |
   | CRITICAL | input: line_charge | 6.1918 | 0.25 |
   | CRITICAL | input: claim_total_charge | 3.6712 | 0.25 |
   | CRITICAL | input: payer_cpt_denial_rate_90d | 0.2851 | 0.25 |
   | WARNING | input: payer_cpt_window_size | 0.1228 | 0.10 |
   | CRITICAL | input: payer_provider_denial_rate_90d | 0.3502 | 0.25 |
   | CRITICAL | input: payer_provider_window_size | 0.3089 | 0.25 |
   | CRITICAL | input: cpt_modifier_denial_rate_90d | 0.2981 | 0.25 |
   | CRITICAL | output: score | 1.0433 | 0.25 |
   | CRITICAL | calibration: ECE | 0.1732 | 0.10 |

   **Severity CRITICAL, action REVERTED.** `ml_deployment` gained
   `v1 REVERTED by monitor:auto`, with every critical breach in its reason.
5. **The next request** for a score got `"withdrawn; this work is on rules
   only"`, with 0 scores returned and 0 new predictions recorded.

```
GATE M6: PASS
```

The trailing-rate inputs breach too, because the drifted lines come from the
training months, whose 90-day windows were still filling. The payer and the
charges are what was simulated, and they breach by an order of magnitude
more.

## Natural drift, measured (M6.7)

The same monitor on real windows of the full corpus, without simulation, in
rolled-back transactions:

| Window | ML-1 | ML-3 |
|---|---|---|
| Oct–Nov 2025 (inside the reference) | OK; ECE 0.011, output PSI 0.002 | WARNING: one input at PSI 0.14 |
| Jan–Mar 2026 (unseen) | CRITICAL: `days_dos_to_submission` PSI 3.06; ECE 0.020 | not judged: 149 predictions, under 200 |

The corpus's encounters end in December 2025, so 2026 holds only late
claims. Reverting ML-1 there is the monitor working on a real change of
population. Its calibration held, and promoting it again is a person's
decision.

Two choices came from these measurements (ADR-0028):

- **The reference is the validation and test windows, not training.**
  Against training, Oct–Nov 2025 showed trailing-window PSI up to 0.57
  while calibration error was 0.011. A good model would have been reverted.
- **The calendar features are not judged for drift.** `submission_month`
  scored PSI 6.4: a later window is always a later month.

## Invariant tests for M6

| Directive / requirement | Test | Status |
|---|---|---|
| FR-ML-08, GATE M6 | `test_critical_drift_auto_reverts` | pass |
| FR-ML-06, M6.9 | `test_model_degradation_falls_back_to_rules` (queues), `test_missing_features_route_to_rules_never_a_guess` | pass |
| PD-5 | `test_rules_override_model_on_conflict`, `test_the_scrub_consults_the_active_model_and_rules_still_decide` | pass |
| FR-ML-05, PD-1 | `test_a_prediction_persists_its_inputs_score_and_shap_drivers` | pass |
| FR-ML-04 | `test_a_shadow_score_is_recorded_and_not_used`, `test_no_model_is_promoted_before_its_shadow_period` | pass |
| PD-20 | `test_another_clients_line_is_never_scored` | pass |
| Evidence is not rewritten | `test_serving_records_reject_update_and_delete` | pass |
| The alarm is not noise | `test_a_model_on_the_population_it_learned_is_not_reverted`, `test_too_few_predictions_are_not_judged` | pass |
| PD-11, FR-ML-01 | the leakage suite (`tests/leakage`) | pass |
| FR-ML-07 | the hard-coded-threshold check in `tests/unit/test_ml_training.py` | pass |

**Negatives shown.** With the revert in `monitoring.run` disabled,
`test_critical_drift_auto_reverts` fails: the run comes back
`('CRITICAL', 'NONE')`. With the missing-feature check in `serving` disabled,
`test_missing_features_route_to_rules_never_a_guess` fails: the line is
scored.

## What this does not show, stated plainly

- **The models learned the generator.** Their numbers say the pipeline
  works, not that a payer behaves this way.
- **The live scrub does not get an ML-1 score yet.** A claim scrubbed before
  it is sent is not in the warehouse, so ML-1 records it DEGRADED and the
  rules' verdict stands. Scoring at submission needs point-in-time features
  for a claim not yet in the mart (ADR-0028).
- **ML-5 is not built** (ADR-0029).
- **Every drift and threshold value is a placeholder** (`thresholds.toml`
  version 3, `decided = false`), awaiting the sponsor.
