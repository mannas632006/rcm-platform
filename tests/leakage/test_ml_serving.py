"""Serving, shadow, drift and the auto-revert, on a real warehouse (M6.4,
M6.6-M6.9; ADR-0028).

These need what the leakage suite already builds: the marts, a published
ruleset and trained models. Time is passed in everywhere, so a shadow period
and a monitoring window are crossed without waiting for them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import ProgrammingError

from rcm.analytics.dataset import (
    DENIAL_RISK_FEATURES,
    OPTIONAL_FEATURES,
    LineRow,
    category_lines,
    denial_lines,
    recovery_lines,
    serving_lines,
)
from rcm.analytics.features import Stage
from rcm.decision import scoring
from rcm.decision.ml import (
    deployment,
    monitoring,
    recovery,
    registry,
    serving,
    shadow,
    thresholds,
)
from rcm.decision.ml.encoding import Encoding
from rcm.decision.ml.model import fit_binary
from rcm.decision.ml.pipeline import (
    TrainingReport,
    train_denial_risk,
    train_recovery,
)
from rcm.decision.ml.registry import CodeVersion
from rcm.decision.ml.registry_models import DataSource, Verdict
from rcm.decision.ml.serving_models import (
    DENIAL_RISK,
    RECOVERY,
    Deployment,
    MonitorAction,
    Prediction,
    PredictionStatus,
    Severity,
)
from rcm.decision.ml.submission import combine
from rcm.decision.rules.evaluate import Verdict as ScrubVerdict
from rcm.decision.rules.scrub import scrub_claim
from rcm.decision.rules.store import publish_ruleset
from rcm.platform.audit import DecisionRecord
from rcm.platform.db import session_scope
from rcm.platform.errors import ValidationFailedError
from tests.leakage.warehouse import CLIENT

pytestmark = pytest.mark.leakage

CODE = CodeVersion(commit="0" * 40, dirty=True)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
BY = "test:ml-serving"


@pytest.fixture(scope="module")
def models(warehouse: Engine, raw_archive: Path) -> Iterator[dict[str, TrainingReport]]:
    """ML-1 and ML-3, trained on the smoke corpus as `test_ml_pipeline` does."""
    with session_scope() as session:
        publish_ruleset(session, published_by=BY)
    reports: dict[str, TrainingReport] = {}
    with session_scope() as session:
        reports[DENIAL_RISK] = train_denial_risk(
            session,
            archive_root=raw_archive,
            code=CODE,
            trained_by=BY,
            today=T0.date(),
            months=(3, 1, 1),
        )
    with session_scope() as session:
        reports[RECOVERY] = train_recovery(
            session, archive_root=raw_archive, code=CODE, trained_by=BY, months=(2, 2, 3)
        )
    yield reports
    with warehouse.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE ml_monitor_run, ml_prediction, ml_deployment, ml_model_version, "
                "ruleset_member, ruleset CASCADE"
            )
        )


@pytest.fixture(scope="module")
def risk(models: dict[str, TrainingReport]) -> TrainingReport:
    report = models[DENIAL_RISK]
    if report.verdict is not Verdict.ACCEPTED:
        pytest.fail(f"ML-1 was {report.verdict} on the smoke corpus: {report.reason}")
    return report


def _lines(
    claims: int | None = None, *, window: tuple[int, int] | None = None
) -> list[LineRow[None]]:
    """ML-1's serving rows, optionally only those in a slice of the months."""
    with session_scope() as session:
        dataset = denial_lines(session)
        rows = sorted(dataset.rows, key=lambda r: (r.submission_date, r.service_line_id))
        if window is not None:
            months = sorted({(r.submission_date.year, r.submission_date.month) for r in rows})
            wanted = set(months[window[0] : window[1]])
            rows = [r for r in rows if (r.submission_date.year, r.submission_date.month) in wanted]
        claim_ids = list(dict.fromkeys(r.claim_id for r in rows))[:claims]
        return list(
            serving_lines(
                session,
                features=DENIAL_RISK_FEATURES,
                stage=Stage.SUBMISSION,
                claim_ids=claim_ids,
                denied_only=False,
            ).rows
        )


def _count(model_name: str, status: PredictionStatus | None = None) -> int:
    with session_scope() as session:
        query = (
            select(func.count()).select_from(Prediction).where(Prediction.model_name == model_name)
        )
        if status is not None:
            query = query.where(Prediction.status == str(status))
        return int(session.scalar(query) or 0)


def _decisions() -> int:
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(DecisionRecord)
                .where(DecisionRecord.decision_type == "PREDICT")
            )
            or 0
        )


# ---------------------------------------------------------------------------
# M6.9: which features a line may lack
# ---------------------------------------------------------------------------


def test_every_required_feature_is_on_every_training_row(warehouse: Engine) -> None:
    """`OPTIONAL_FEATURES` is held to the data: a feature outside it that a
    training row lacks would make the degradation rule refuse what the model
    was in fact trained on."""
    with session_scope() as session:
        for dataset in (denial_lines(session), recovery_lines(session), category_lines(session)):
            assert dataset.rows
            for index, feature in enumerate(dataset.features):
                if feature.name not in OPTIONAL_FEATURES:
                    missing = sum(1 for row in dataset.rows if row.values[index] is None)
                    assert missing == 0, f"{feature.name} is missing on {missing} training rows"


# ---------------------------------------------------------------------------
# M6.6 and M6.4: shadow records everything and returns nothing
# ---------------------------------------------------------------------------


def test_only_an_accepted_version_enters_shadow(risk: TrainingReport, raw_archive: Path) -> None:
    """The same rows and code do not beat themselves: v2 is REJECTED, and a
    rejected model is never served."""
    with session_scope() as session:
        second = train_denial_risk(
            session,
            archive_root=raw_archive,
            code=CODE,
            trained_by=BY,
            today=T0.date(),
            months=(3, 1, 1),
        )
    assert second.verdict is Verdict.REJECTED
    with session_scope() as session, pytest.raises(ValidationFailedError) as refused:
        deployment.start_shadow(
            session,
            model_name=DENIAL_RISK,
            version=second.version,
            changed_by=BY,
            reason="should be refused",
            now=T0,
        )
    assert "REJECTED" in refused.value.context["reason"]


def test_a_shadow_score_is_recorded_and_not_used(risk: TrainingReport) -> None:
    """M6.6: "Model output recorded, not used"."""
    rows = _lines(claims=20)
    claim_ids = list(dict.fromkeys(r.claim_id for r in rows))
    with session_scope() as session:
        nothing = serving.score(session, DENIAL_RISK, client_id=CLIENT, claim_ids=claim_ids, now=T0)
        assert nothing.unavailable == serving.NOT_DEPLOYED
        deployment.start_shadow(
            session,
            model_name=DENIAL_RISK,
            version=risk.version,
            changed_by=BY,
            reason="accepted; enters shadow",
            now=T0,
        )
        served = serving.score(session, DENIAL_RISK, client_id=CLIENT, claim_ids=claim_ids, now=T0)
    assert served.unavailable == serving.IN_SHADOW
    assert served.scores == {}
    assert _count(DENIAL_RISK, PredictionStatus.SCORED) == len(rows)


def test_a_prediction_persists_its_inputs_score_and_shap_drivers(risk: TrainingReport) -> None:
    """FR-ML-05 and PD-1, for a model's decisions."""
    names = [f.name for f in DENIAL_RISK_FEATURES]
    with session_scope() as session:
        pairs = session.execute(
            select(Prediction, DecisionRecord)
            .join(DecisionRecord, DecisionRecord.decision_id == Prediction.decision_id)
            .where(Prediction.model_name == DENIAL_RISK)
        ).all()
        assert pairs
        for prediction, decision in pairs:
            assert decision.decided_by == "model:ML-1"
            assert decision.logic_version == f"ML-1 v{risk.version}"
            assert decision.decision_type == "PREDICT"
            assert (decision.entity_type, decision.entity_id) == (
                "service_line",
                prediction.service_line_id,
            )
            assert sorted(decision.input_snapshot["features"]) == sorted(names)
            output = decision.output
            assert output["score"] == prediction.score == decision.confidence
            assert output["mode"] == "SHADOW"
            assert output["used"] is False
            assert output["thresholds_version"] == thresholds.load().version
            assert output["route"] in (serving.ROUTE_REVIEW, serving.ROUTE_PASS)
            drivers = output["drivers"]
            assert 1 <= len(drivers) <= serving.TOP_DRIVERS
            assert all(d["feature"] in names for d in drivers)
            sizes = [abs(d["contribution"]) for d in drivers]
            assert sizes == sorted(sizes, reverse=True)


def test_a_line_is_scored_once_per_version(risk: TrainingReport) -> None:
    """Opening a queue twice does not make two decisions of one."""
    before = (_count(DENIAL_RISK), _decisions())
    rows = _lines(claims=20)
    with session_scope() as session:
        serving.score(
            session,
            DENIAL_RISK,
            client_id=CLIENT,
            claim_ids=list(dict.fromkeys(r.claim_id for r in rows)),
            now=T0 + timedelta(hours=1),
        )
    assert (_count(DENIAL_RISK), _decisions()) == before


def test_another_clients_line_is_never_scored(risk: TrainingReport) -> None:
    """PD-20: scoping is in the read, whatever ids are asked for."""
    rows = _lines(claims=40)
    before = _count(DENIAL_RISK, PredictionStatus.SCORED)
    with session_scope() as session:
        serving.score(
            session,
            DENIAL_RISK,
            client_id=uuid.uuid4(),
            claim_ids=list(dict.fromkeys(r.claim_id for r in rows)),
            now=T0,
        )
    assert _count(DENIAL_RISK, PredictionStatus.SCORED) == before


# ---------------------------------------------------------------------------
# M6.9: graceful degradation
# ---------------------------------------------------------------------------


def test_missing_features_route_to_rules_never_a_guess(risk: TrainingReport) -> None:
    """FR-ML-06: a line without a feature the model always had is not scored."""
    charge = [f.name for f in DENIAL_RISK_FEATURES].index("line_charge")
    whole = _lines(claims=60)[-1]
    broken = replace(
        whole, values=tuple(None if i == charge else v for i, v in enumerate(whole.values))
    )
    with session_scope() as session:
        state = deployment.states(session, DENIAL_RISK)[risk.version]
        scores = serving.score_rows(session, deployed=state, rows=[broken], now=T0)
    assert broken.service_line_id not in scores
    with session_scope() as session:
        row = session.scalar(
            select(Prediction).where(Prediction.service_line_id == broken.service_line_id)
        )
        assert row is not None
        assert (row.status, row.score, row.decision_id) == ("DEGRADED", None, None)
        assert row.reason == "missing: line_charge"


def test_a_claim_the_warehouse_does_not_hold_is_degraded_once(risk: TrainingReport) -> None:
    unknown = uuid.uuid4()
    for _ in range(2):
        with session_scope() as session:
            serving.score(session, DENIAL_RISK, client_id=CLIENT, claim_ids=[unknown], now=T0)
    with session_scope() as session:
        rows = session.scalars(select(Prediction).where(Prediction.claim_id == unknown)).all()
        assert [(r.status, r.reason) for r in rows] == [("DEGRADED", serving.NO_FEATURE_ROW)]


def test_an_optional_feature_may_be_missing(risk: TrainingReport) -> None:
    """A line with no modifier is one the model was trained on."""
    with session_scope() as session:
        model = serving.loaded(deployment.accepted_version(session, DENIAL_RISK, risk.version))
    values = tuple(None if f.name in OPTIONAL_FEATURES else 1.0 for f in DENIAL_RISK_FEATURES)
    assert serving.missing_features(model, values) == []


# ---------------------------------------------------------------------------
# FR-ML-04: promotion needs the shadow period
# ---------------------------------------------------------------------------


def test_no_model_is_promoted_before_its_shadow_period(risk: TrainingReport) -> None:
    days = thresholds.load().shadow_min_days
    with session_scope() as session, pytest.raises(ValidationFailedError) as early:
        deployment.promote(
            session,
            model_name=DENIAL_RISK,
            version=risk.version,
            changed_by=BY,
            reason="too early",
            now=T0 + timedelta(days=days - 1),
        )
    assert f"in shadow {days - 1} days of the {days} required" in early.value.context["reason"]
    with session_scope() as session:
        deployment.promote(
            session,
            model_name=DENIAL_RISK,
            version=risk.version,
            changed_by=BY,
            reason="shadow period served",
            now=T0 + timedelta(days=days),
        )
        rows = _lines(claims=20)
        served = serving.score(
            session,
            DENIAL_RISK,
            client_id=CLIENT,
            claim_ids=list(dict.fromkeys(r.claim_id for r in rows)),
            now=T0 + timedelta(days=days),
        )
    assert served.unavailable is None
    assert set(served.scores) == {r.service_line_id for r in rows}
    assert all(0.0 <= s <= 1.0 for s in served.scores.values())


def test_the_scrub_consults_the_active_model_and_rules_still_decide(
    risk: TrainingReport,
) -> None:
    """ML-1 on the live path (PD-5): the scrub records the rules' verdict, the
    model's score, and the verdict they make together, which the model can
    only raise from PASS to ADVISE."""
    claim_ids = list(dict.fromkeys(r.claim_id for r in _lines(claims=10)))
    review_at = thresholds.load().review_threshold(DENIAL_RISK)
    with session_scope() as session:
        for claim_id in claim_ids:
            result = scrub_claim(session, claim_id=claim_id, client_id=CLIENT, today=T0.date())
            assert result.denial_risk is not None
            score = result.denial_risk.score
            assert score is not None, result.denial_risk.unavailable
            assert result.verdict == combine(result.evaluation.verdict, score)
            if result.evaluation.verdict == ScrubVerdict.PASS:
                expected = ScrubVerdict.ADVISE if score >= review_at else ScrubVerdict.PASS
                assert result.verdict == expected
            else:
                assert result.verdict == result.evaluation.verdict
            record = session.scalars(
                select(DecisionRecord)
                .where(
                    DecisionRecord.entity_id == claim_id, DecisionRecord.decision_type == "SCRUB"
                )
                .order_by(DecisionRecord.decided_at.desc())
            ).first()
            assert record is not None
            output = record.output
            assert output["rules_verdict"] == str(result.evaluation.verdict)
            assert output["verdict"] == str(result.verdict)
            assert output["denial_risk"]["score"] == score
        # The scrub corrects claims it passes; this test keeps none of it.
        session.rollback()


# ---------------------------------------------------------------------------
# M6.7 and M6.8: drift, and the revert nobody has to pull
# ---------------------------------------------------------------------------


def _drifted(rows: list[LineRow[None]]) -> list[LineRow[None]]:
    """The same lines from a different world: one unknown payer, and every
    charge twenty times what the model learned on."""
    names = [f.name for f in DENIAL_RISK_FEATURES]
    payer, line, claim = (names.index(n) for n in ("payer_id", "line_charge", "claim_total_charge"))

    def shift(row: LineRow[None]) -> LineRow[None]:
        values = list(row.values)
        values[payer] = "NEWPAYER"
        for index in (line, claim):
            charged = values[index]
            assert isinstance(charged, float)
            values[index] = charged * 20
        return replace(row, values=tuple(values))

    return [shift(r) for r in rows]


def test_a_model_on_the_population_it_learned_is_not_reverted(risk: TrainingReport) -> None:
    """The negative: the monitor does not fire on the lines the model was
    validated and tested on, so when it fires below it is the drift that fired it."""
    quiet = T0 + timedelta(days=100)
    # The months the model was validated and tested on: its reference.
    rows = _lines(window=(3, 5))
    assert len(rows) >= thresholds.load().drift.min_predictions
    with session_scope() as session:
        state = deployment.states(session, DENIAL_RISK)[risk.version]
        assert state.status == "ACTIVE"
        serving.score_rows(session, deployed=state, rows=rows, now=quiet)
        run = monitoring.run(session, model_name=DENIAL_RISK, version=risk.version, now=quiet)
        assert run.metrics["judged"] is True
        assert run.metrics["coverage"] >= thresholds.load().drift.min_coverage
        assert run.severity != str(Severity.CRITICAL), run.breaches
        assert run.action == str(MonitorAction.NONE)
        assert deployment.states(session, DENIAL_RISK)[risk.version].status == "ACTIVE"


def test_critical_drift_auto_reverts(risk: TrainingReport) -> None:
    """FR-ML-08, GATE M6: under drift the model path disables itself, with
    nobody asked, and stays disabled."""
    later = T0 + timedelta(days=200)
    # Lines the quiet test did not score: a line is scored once per version.
    rows = _drifted(_lines(window=(0, 3)))
    assert len(rows) >= thresholds.load().drift.min_predictions
    with session_scope() as session:
        state = deployment.states(session, DENIAL_RISK)[risk.version]
        serving.score_rows(session, deployed=state, rows=rows, now=later)

    # The daily monitor, as beat runs it: no version named, nobody consulted.
    with session_scope() as session:
        runs = [r for r in monitoring.run_all(session, now=later) if r.model_name == DENIAL_RISK]
        assert [(r.severity, r.action) for r in runs] == [("CRITICAL", "REVERTED")]
        critical = {b["metric"] for b in runs[0].breaches if b["severity"] == "CRITICAL"}
        assert {"input:payer_id", "input:line_charge"} <= critical

    with session_scope() as session:
        last = deployment.latest(session, DENIAL_RISK)
        assert last is not None
        assert (last.status, last.changed_by) == ("REVERTED", monitoring.MONITOR)
        assert "input:payer_id" in last.reason

    before = _count(DENIAL_RISK)
    claim_ids = list(dict.fromkeys(r.claim_id for r in _lines(claims=20)))
    with session_scope() as session:
        served = serving.score(
            session, DENIAL_RISK, client_id=CLIENT, claim_ids=claim_ids, now=later
        )
        assert served.unavailable == serving.REVERTED
        assert served.scores == {}
        # Withdrawn means not monitored either: there is nothing left to revert.
        again = [r for r in monitoring.run_all(session, now=later) if r.model_name == DENIAL_RISK]
        assert again == []
    assert _count(DENIAL_RISK) == before


def test_too_few_predictions_are_not_judged(risk: TrainingReport) -> None:
    """A model is not withdrawn on a handful of rows."""
    with session_scope() as session:
        run = monitoring.run(
            session, model_name=DENIAL_RISK, version=risk.version, now=T0 + timedelta(days=900)
        )
        assert run.metrics["judged"] is False
        assert (run.severity, run.breaches) == ("OK", [])


# ---------------------------------------------------------------------------
# ML-3 behind the queues' seam (ADR-0020)
# ---------------------------------------------------------------------------


def _a_served_recovery_model() -> int:
    """An ML-3 version the registry will serve.

    The smoke corpus has too few recovered denials for ML-3 to beat the
    queue prior, so the judge rejects it there, rightly, and a rejected
    version is never served. The judge is tested in `test_ml_pipeline`; what
    is under test here is serving. So this fits ML-3 on the same rows and
    registers it ACCEPTED by hand, saying so in its reason.
    """
    with session_scope() as session:
        dataset = recovery_lines(session)
        rows = sorted(dataset.rows, key=lambda r: r.submission_date)
        cut = len(rows) * 2 // 3
        train, valid = rows[:cut], rows[cut:]
        model = fit_binary(
            Encoding.fit(dataset.features, (r.values for r in train)),
            ([r.values for r in train], [r.label for r in train]),
            ([r.values for r in valid], [r.label for r in valid]),
        )
        row = registry.register(
            session,
            model_name=RECOVERY,
            model=model,
            verdict=Verdict.ACCEPTED,
            reason="test fixture: registered by hand to test serving, not judged",
            data_source=DataSource.SYNTHETIC,
            data_hash=dataset.data_hash(),
            code=CODE,
            thresholds_version=thresholds.load().version,
            params={},
            metrics={},
            baseline={},
            windows={
                "train_start": train[0].submission_date.isoformat(),
                "valid_start": valid[0].submission_date.isoformat(),
            },
            trained_by=BY,
        )
        return row.version


def test_the_queue_seam_says_why_ml3_has_no_scores_then_gives_them(
    models: dict[str, TrainingReport],
) -> None:
    version = _a_served_recovery_model()
    with session_scope() as session:
        denied = recovery_lines(session).rows
    claim_ids = list(dict.fromkeys(r.claim_id for r in denied))[:30]
    scoring.deploy_recovery_model(recovery.ServedRecovery())
    try:
        with session_scope() as session:
            scores = scoring.recovery_scores(session, client_id=CLIENT, claim_ids=claim_ids)
            assert scores.unavailable == scoring.NOT_DEPLOYED
            deployment.start_shadow(
                session,
                model_name=RECOVERY,
                version=version,
                changed_by=BY,
                reason="accepted; enters shadow",
                now=T0,
            )
            scores = scoring.recovery_scores(session, client_id=CLIENT, claim_ids=claim_ids)
            assert (scores.unavailable, dict(scores.values)) == (recovery.IN_SHADOW, {})
            deployment.promote(
                session,
                model_name=RECOVERY,
                version=version,
                changed_by=BY,
                reason="shadow period served",
                now=T0 + timedelta(days=thresholds.load().shadow_min_days),
            )
            scores = scoring.recovery_scores(session, client_id=CLIENT, claim_ids=claim_ids)
            assert scores.unavailable is None
            assert set(scores.values) == set(claim_ids)
            assert all(0.0 <= s <= 1.0 for s in scores.values.values())
            comparison = shadow.compare(
                session, model_name=RECOVERY, version=version, today=T0.date()
            )
            assert comparison["matured"] > 0
            assert "queue prior" in comparison["rivals"]
            deployment.revert(
                session,
                model_name=RECOVERY,
                version=version,
                changed_by=BY,
                reason="withdrawn by hand",
                now=T0 + timedelta(days=60),
            )
            scores = scoring.recovery_scores(session, client_id=CLIENT, claim_ids=claim_ids)
            assert (scores.unavailable, dict(scores.values)) == (recovery.REVERTED, {})
    finally:
        scoring.deploy_recovery_model(None)


# ---------------------------------------------------------------------------
# The records are evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ml_deployment SET reason = 'rewritten'",
        "DELETE FROM ml_deployment",
        "UPDATE ml_prediction SET score = 0",
        "DELETE FROM ml_prediction",
        "UPDATE ml_monitor_run SET severity = 'OK'",
        "DELETE FROM ml_monitor_run",
    ],
)
def test_serving_records_reject_update_and_delete(risk: TrainingReport, statement: str) -> None:
    """Insert-only by grant: the application role cannot rewrite why a model
    was withdrawn, or what it scored."""
    with pytest.raises(ProgrammingError, match="permission denied"), session_scope() as session:
        session.execute(text(statement))
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Deployment))
