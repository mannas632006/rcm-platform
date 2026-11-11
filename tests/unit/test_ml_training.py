"""Training ML-1: the gate before it, the split, the model, and the verdict.

M6.1, M6.2, M6.5 (ADR-0026). The database half -- reading the mart,
provenance, the registry -- is in `tests/leakage/test_ml_pipeline.py`.
"""

from __future__ import annotations

import ast
import random
import warnings
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from rcm.analytics.dataset import DENIAL_RISK_FEATURES, Feature, Kind, Value
from rcm.analytics.features import assert_trainable
from rcm.analytics.training import temporal_windows
from rcm.decision.ml import thresholds
from rcm.decision.ml.encoding import Encoding
from rcm.decision.ml.metrics import expected_calibration_error, population_stability_index
from rcm.decision.ml.model import fit_binary
from rcm.decision.ml.pipeline import _verdict, per_payer_table
from rcm.decision.ml.registry_models import Verdict
from rcm.decision.ml.tracking import MlflowTracker, flat_metrics
from rcm.platform.errors import LeakageError, ValidationFailedError

ML_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "rcm" / "decision" / "ml"

# ---------------------------------------------------------------------------
# FR-ML-01: the gate before a row is read
# ---------------------------------------------------------------------------


def test_ml1_trains_only_on_features() -> None:
    assert_trainable(f.name for f in DENIAL_RISK_FEATURES)


@pytest.mark.invariant
@pytest.mark.parametrize(
    ("column", "said"),
    [
        ("is_denied", "LABEL"),
        ("carc_code", "LABEL"),
        ("claim_id", "KEY"),
        ("outcome_matured_at", "PROVENANCE"),
        ("auth_required", "no source yet"),
        ("cpt_descriptor", "not a mart column"),
    ],
)
def test_training_refuses_what_is_not_a_feature(column: str, said: str) -> None:
    """A label, a key or an always-null column never reaches a model."""
    with pytest.raises(LeakageError) as raised:
        assert_trainable(["payer_id", column])
    assert raised.value.context["column"] == column
    assert said in raised.value.context["reason"]


# ---------------------------------------------------------------------------
# FR-ML-02: 18 / 3 / 3 months, by date
# ---------------------------------------------------------------------------


def test_the_split_is_eighteen_three_three_months() -> None:
    windows = temporal_windows(date(2024, 1, 17))
    assert windows.train_start == date(2024, 1, 1)
    assert windows.valid_start == date(2025, 7, 1)
    assert windows.test_start == date(2025, 10, 1)
    assert windows.test_end == date(2026, 1, 1)


def test_a_window_of_no_months_is_refused() -> None:
    with pytest.raises(LeakageError):
        temporal_windows(date(2024, 1, 1), test_months=0)


# ---------------------------------------------------------------------------
# M6.5: thresholds are versioned config
# ---------------------------------------------------------------------------


def test_the_thresholds_ship_with_a_version_and_say_they_are_undecided() -> None:
    config = thresholds.load()
    assert config.version >= 1
    # ADR-0026: every value is a placeholder until the sponsor sets it.
    assert config.decided is False
    assert 0 <= config.review_threshold("ML-1") <= 1


def _raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "decided": False,
        "acceptance": {"min_pr_auc_lift": 0.0, "min_macro_f1_lift": 0.0},
        "models": {"ML-1": {"review_at": 0.5}},
        "drift": {
            "psi_warning": 0.1,
            "psi_critical": 0.25,
            "ece_critical": 0.1,
            "min_coverage": 0.9,
            "window_days": 30,
            "min_predictions": 200,
        },
        "shadow": {"min_days": 28},
    }
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    "broken",
    [
        {"version": None},
        {"decided": "no"},
        {"models": {"ML-1": {"review_at": 1.5}}},
        {"models": {"ML-1": {}}},
        {"drift": {"psi_warning": 0.1}},
        {"shadow": {}},
        {"acceptance": {"min_pr_auc_lift": 0.0}},
    ],
)
def test_a_threshold_file_missing_a_value_is_refused(broken: dict[str, Any]) -> None:
    """PD-18: no value falls back to a default the file did not give."""
    thresholds.parse(_raw())
    with pytest.raises(ValidationFailedError):
        thresholds.parse(_raw(**broken))


def _hard_coded(source: str) -> list[int]:
    """Lines comparing anything against a numeric literal other than 0 or 1."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                value = operand.operand if isinstance(operand, ast.UnaryOp) else operand
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, int | float)
                    and not isinstance(value.value, bool)
                    and value.value not in (0, 1)
                ):
                    found.append(node.lineno)
    return found


@pytest.mark.invariant
def test_no_hard_coded_threshold() -> None:
    """M6.5: a decision threshold in `rcm.decision.ml` comes from the
    thresholds file, never a literal in a comparison (FR-ML-07)."""
    offenders = {
        path.name: lines
        for path in sorted(ML_PACKAGE.glob("*.py"))
        if (lines := _hard_coded(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_the_threshold_check_would_notice_one() -> None:
    """The negative: the scan finds `>= 0.7` and its line."""
    assert _hard_coded("def route(p):\n    if p >= 0.7:\n        return 'review'\n") == [2]
    assert _hard_coded("ok = score > threshold and count >= 1\n") == []


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

FEATURES = (
    Feature("payer_id", Kind.CATEGORY),
    Feature("cpt_hcpcs", Kind.CATEGORY),
    Feature("auth_number_present", Kind.FLAG),
    Feature("line_charge", Kind.NUMBER),
)


def _rows(n: int, *, seed: int) -> tuple[list[tuple[Value, ...]], list[bool]]:
    """Denials driven by a missing authorisation on one payer, plus noise."""
    rng = random.Random(seed)
    rows: list[tuple[Value, ...]] = []
    labels: list[bool] = []
    for _ in range(n):
        payer = rng.choice(["60054", "00590", "87726"])
        auth = rng.random() < 0.8
        charge = round(rng.uniform(50, 900), 2)
        rows.append((payer, rng.choice(["99213", "99214", "90837"]), 1.0 if auth else 0.0, charge))
        denied = (payer == "60054" and not auth) or rng.random() < 0.05
        labels.append(denied)
    return rows, labels


def _fit() -> Any:
    train = _rows(1500, seed=1)
    valid = _rows(500, seed=2)
    encoding = Encoding.fit(FEATURES, train[0])
    return fit_binary(encoding, train, valid)


def test_the_same_rows_give_the_same_model() -> None:
    """PD-10, FR-ML-03: a model is rebuildable from its data and code."""
    assert _fit().text() == _fit().text()


def test_the_model_learns_the_pattern_it_was_given() -> None:
    model = _fit()
    risky, safe = model.predict([("60054", "99213", 0.0, 200.0), ("00590", "99213", 1.0, 200.0)])
    assert risky > safe


def test_a_code_first_seen_after_training_is_missing_not_new() -> None:
    encoding = Encoding.fit(FEATURES, [("60054", "99213", 1.0, 10.0)])
    matrix = encoding.matrix([("99999", "99213", 1.0, 10.0)])
    assert np.isnan(matrix[0, 0])
    assert matrix[0, 1] == 0


def test_the_encoding_survives_the_registry() -> None:
    encoding = Encoding.fit(FEATURES, _rows(50, seed=3)[0])
    assert Encoding.from_json(encoding.to_json()) == encoding


def test_drivers_are_shap_values() -> None:
    """M6.4's drivers are LightGBM's TreeSHAP; the `shap` package agrees."""
    shap = pytest.importorskip("shap")
    model = _fit()
    rows = _rows(20, seed=4)[0]
    ours = model.booster.predict(model.encoding.matrix(rows), pred_contrib=True)[:, :-1]
    with warnings.catch_warnings():
        # shap's own notice that its LightGBM output shape changed; handled below.
        warnings.filterwarnings(
            "ignore", message="LightGBM binary classifier", category=UserWarning
        )
        theirs = shap.TreeExplainer(model.booster).shap_values(model.encoding.matrix(rows))
    theirs = theirs[1] if isinstance(theirs, list) else theirs
    np.testing.assert_allclose(ours, theirs, atol=1e-6)
    top = model.drivers(rows[:1], top=2)[0]
    assert len(top) == 2
    assert abs(top[0].contribution) >= abs(top[1].contribution)


# ---------------------------------------------------------------------------
# M6.2: beat the rules or be rejected
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_model_that_does_not_beat_the_rules_is_rejected() -> None:
    verdict, reason = _verdict({"pr_auc": 0.30}, {"rules baseline": {"pr_auc": 0.35}}, lift=0.0)
    assert verdict is Verdict.REJECTED
    assert "does not beat rules baseline 0.3500" in reason


def test_a_model_must_beat_the_current_one_too() -> None:
    verdict, _ = _verdict(
        {"pr_auc": 0.40},
        {"rules baseline": {"pr_auc": 0.20}, "v1": {"pr_auc": 0.45}},
        lift=0.0,
    )
    assert verdict is Verdict.REJECTED


def test_a_model_that_beats_both_is_accepted() -> None:
    verdict, reason = _verdict(
        {"pr_auc": 0.50},
        {"rules baseline": {"pr_auc": 0.20}, "v1": {"pr_auc": 0.45}},
        lift=0.0,
    )
    assert verdict is Verdict.ACCEPTED
    assert reason.startswith("PR-AUC 0.5000")


def test_a_tie_with_the_rules_is_not_a_win() -> None:
    verdict, _ = _verdict({"pr_auc": 0.3}, {"rules baseline": {"pr_auc": 0.3}}, lift=0.0)
    assert verdict is Verdict.REJECTED


# ---------------------------------------------------------------------------
# Calibration and stability (M6.2 reports them; M6.7 alarms on them)
# ---------------------------------------------------------------------------


def test_a_calibrated_model_has_no_calibration_error() -> None:
    assert expected_calibration_error([0.25] * 4, [1, 0, 0, 0]) == pytest.approx(0.0)
    assert expected_calibration_error([0.9] * 10, [0] * 10) == pytest.approx(0.9)


def test_psi_is_zero_for_the_same_sample_and_grows_with_a_shift() -> None:
    rng = np.random.default_rng(7)
    reference = rng.normal(0, 1, 5000)
    edges = np.quantile(reference, np.linspace(0, 1, 11))
    edges[0], edges[-1] = -np.inf, np.inf
    assert population_stability_index(reference, reference, edges=edges) == pytest.approx(0.0)
    shifted = population_stability_index(reference, rng.normal(1, 1, 5000), edges=edges)
    assert shifted > population_stability_index(reference, rng.normal(0.1, 1, 5000), edges=edges)


# ---------------------------------------------------------------------------
# The report and its tracking (FR-ML-03)
# ---------------------------------------------------------------------------


def test_a_model_without_a_review_threshold_is_refused() -> None:
    with pytest.raises(ValidationFailedError):
        thresholds.load().review_threshold("ML-9")


def test_the_per_payer_table_puts_the_biggest_payer_first() -> None:
    report = {
        "per_payer": {
            "00590": {"rows": 40, "positives": 4, "pr_auc": 0.5},
            "60054": {"rows": 90, "positives": 9, "pr_auc": None},
        }
    }
    assert per_payer_table(report) == [("60054", 90, 9, None), ("00590", 40, 4, 0.5)]


def test_only_numbers_reach_mlflow() -> None:
    report = {"pr_auc": 0.4, "rows": 10, "ok": True, "per_payer": {"60054": {"pr_auc": 0.3}}}
    assert flat_metrics(report, prefix="test.") == {
        "test.pr_auc": 0.4,
        "test.rows": 10.0,
        "test.per_payer.60054.pr_auc": 0.3,
    }


# MLflow's own SQLite store uses a loader SQLAlchemy 2.1 deprecates; not our code.
@pytest.mark.filterwarnings("ignore:The ``noload`` loader strategy is deprecated")
def test_a_run_is_tracked_with_its_data_hash_and_commit(tmp_path: Path) -> None:
    mlflow = pytest.importorskip("mlflow")
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    run_id = MlflowTracker(uri).record(
        experiment="ML-1",
        params={"learning_rate": 0.05},
        metrics={"test.pr_auc": 0.42},
        tags={"data_hash": "a" * 64, "code_commit": "b" * 40},
        artefact="tree\n",
    )
    run = mlflow.MlflowClient(tracking_uri=uri).get_run(run_id)
    assert run.data.metrics["test.pr_auc"] == pytest.approx(0.42)
    assert run.data.tags["data_hash"] == "a" * 64
    assert run.data.params["learning_rate"] == "0.05"
