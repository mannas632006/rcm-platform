"""The parts of serving and monitoring that need no database (M6.7, PD-5)."""

from __future__ import annotations

import random
import uuid

import pytest

from rcm.analytics.dataset import Feature, Kind, Value
from rcm.decision.ml import monitoring, recovery, serving, thresholds
from rcm.decision.ml.serving_models import DENIAL_RISK, Severity
from rcm.decision.ml.submission import combine
from rcm.decision.rules.evaluate import Verdict

DRIFT = thresholds.load().drift
REVIEW_AT = thresholds.load().review_threshold(DENIAL_RISK)


# ---------------------------------------------------------------------------
# PD-5
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@pytest.mark.parametrize(
    ("rules", "denial_risk", "expected"),
    [
        # The contrived conflict: the rules block, the model is sure it is fine.
        (Verdict.BLOCK, 0.0, Verdict.BLOCK),
        (Verdict.ADVISE, 0.0, Verdict.ADVISE),
        # The other way round: the model may ask for a look, never block.
        (Verdict.PASS, 1.0, Verdict.ADVISE),
        (Verdict.PASS, REVIEW_AT, Verdict.ADVISE),
        (Verdict.PASS, 0.0, Verdict.PASS),
        # No score -- shadow, withdrawn, degraded: the rules' verdict stands.
        (Verdict.PASS, None, Verdict.PASS),
        (Verdict.BLOCK, None, Verdict.BLOCK),
    ],
)
def test_rules_override_model_on_conflict(
    rules: Verdict, denial_risk: float | None, expected: Verdict
) -> None:
    """PD-5: deterministic rules always override model output on conflict."""
    assert combine(rules, denial_risk) is expected


def test_a_model_never_lowers_a_verdict_and_never_blocks() -> None:
    order = [Verdict.PASS, Verdict.ADVISE, Verdict.BLOCK]
    for rules in order:
        for risk in (None, 0.0, 0.25, 0.5, 0.75, 1.0):
            combined = combine(rules, risk)
            assert order.index(combined) >= order.index(rules)
            assert combined is not Verdict.BLOCK or rules is Verdict.BLOCK


# ---------------------------------------------------------------------------
# M6.7: the population stability index of one feature
# ---------------------------------------------------------------------------

NUMBER = Feature("line_charge", Kind.NUMBER)
CODE = Feature("payer_id", Kind.CATEGORY)
EVEN = [1.0, 1.0, 1.0, 1.0]


def _numbers(seed: int, n: int, *, scale: float = 1.0) -> list[Value]:
    rng = random.Random(seed)
    return [rng.lognormvariate(5.0, 1.0) * scale for _ in range(n)]


def _codes(seed: int, n: int, weights: list[float]) -> list[Value]:
    rng = random.Random(seed)
    return list(rng.choices(["A", "B", "C", "D"], weights=weights, k=n))


def test_the_same_population_is_stable() -> None:
    assert monitoring.feature_psi(NUMBER, _numbers(1, 5000), _numbers(2, 2000)) < DRIFT.psi_warning
    codes = monitoring.feature_psi(CODE, _codes(1, 5000, EVEN), _codes(2, 2000, EVEN))
    assert codes < DRIFT.psi_warning


def test_a_shifted_number_is_critical() -> None:
    shifted = monitoring.feature_psi(NUMBER, _numbers(1, 5000), _numbers(2, 2000, scale=20.0))
    assert shifted >= DRIFT.psi_critical


def test_a_code_never_seen_in_training_is_critical() -> None:
    unseen: list[Value] = ["NEWPAYER"] * 2000
    assert monitoring.feature_psi(CODE, _codes(1, 5000, EVEN), unseen) >= DRIFT.psi_critical


def test_a_feature_going_missing_is_drift() -> None:
    """Missing is a bin of its own: no histogram edge would see it."""
    recent: list[Value] = [*_numbers(2, 1000), *([None] * 1000)]
    assert monitoring.feature_psi(NUMBER, _numbers(1, 5000), recent) >= DRIFT.psi_critical


# ---------------------------------------------------------------------------
# M6.7: what one window breaches
# ---------------------------------------------------------------------------


def _measure(
    *,
    recent: list[Value],
    requests: int,
    realised: list[tuple[float, bool]] | None = None,
) -> tuple[dict[str, object], list[monitoring.Breach]]:
    reference = [(v,) for v in _numbers(1, 5000)]
    return monitoring.measure(
        features=[NUMBER],
        reference=reference,
        reference_scores=[0.2] * len(reference),
        recent=[(v,) for v in recent],
        recent_scores=[0.2] * len(recent),
        realised=realised or [],
        requests=requests,
        config=DRIFT,
    )


def test_too_few_requests_are_not_judged() -> None:
    few = DRIFT.min_predictions - 1
    metrics, breaches = _measure(recent=_numbers(2, few, scale=20.0), requests=few)
    assert metrics["judged"] is False
    assert breaches == []


def test_low_coverage_is_critical() -> None:
    """A model that mostly cannot answer is not serving."""
    n = DRIFT.min_predictions * 10
    answered = int(n * DRIFT.min_coverage) - 1
    _, breaches = _measure(recent=_numbers(2, answered), requests=n)
    assert [(b.metric, b.severity) for b in breaches] == [("coverage", Severity.CRITICAL)]


def test_a_model_wrong_about_its_own_confidence_is_critical() -> None:
    n = DRIFT.min_predictions * 10
    # It said 0.2 every time, and every one of them was denied.
    _, breaches = _measure(recent=_numbers(2, n), requests=n, realised=[(0.2, True)] * n)
    assert [(b.metric, b.severity) for b in breaches] == [("calibration:ece", Severity.CRITICAL)]


def test_a_stable_window_breaches_nothing() -> None:
    n = DRIFT.min_predictions * 10
    metrics, breaches = _measure(
        recent=_numbers(2, n), requests=n, realised=[(0.2, i % 5 == 0) for i in range(n)]
    )
    assert metrics["judged"] is True
    assert breaches == []


# ---------------------------------------------------------------------------
# ML-3: lines into a claim
# ---------------------------------------------------------------------------


def test_a_claims_score_weighs_its_lines_by_charge() -> None:
    claim, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    served = serving.Served(
        scores={a: 1.0, b: 0.0}, claim_of={a: claim, b: claim}, charge_of={a: 300.0, b: 100.0}
    )
    assert recovery.claim_scores(served) == {claim: pytest.approx(0.75)}


def test_a_claim_with_an_unscored_line_has_no_score() -> None:
    """FR-ML-06: half an answer is not shown as a whole one."""
    claim, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    served = serving.Served(
        scores={a: 0.9}, claim_of={a: claim, b: claim}, charge_of={a: 300.0, b: 100.0}
    )
    assert recovery.claim_scores(served) == {}
