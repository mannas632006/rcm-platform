"""ML-1, ML-2 and ML-3 end to end, against the warehouse the leakage suite
builds (M6.1-M6.3).

The smoke corpus covers six months, too few for SRS 9.2's 18 / 3 / 3, so the
pipeline runs here on 3 / 1 / 1. Everything else is as `make ml-train` runs
it on the full corpus: the leakage gate, the matured mart, the rules
baseline, provenance read from the archived files, and the registry.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ProgrammingError

from rcm.analytics.dataset import category_lines, denial_lines, recovery_lines
from rcm.decision.ml import registry
from rcm.decision.ml.pipeline import (
    DENIAL_CATEGORY,
    DENIAL_RISK,
    RECOVERY,
    Task,
    TrainingReport,
    train_denial_category,
    train_denial_risk,
    train_recovery,
)
from rcm.decision.ml.registry import CodeVersion
from rcm.decision.ml.registry_models import DataSource, ModelVersion
from rcm.decision.rules.store import publish_ruleset
from rcm.platform.db import session_scope
from rcm.platform.errors import ValidationFailedError

pytestmark = pytest.mark.leakage

SMOKE_MONTHS = (3, 1, 1)
# ML-3's answer is a later paid resubmission, so recoveries thin out towards
# the end of the six-month smoke corpus (May has none). Wider valid and test
# windows give every window both answers; the full corpus needs no such care.
RECOVERY_MONTHS = (2, 2, 3)
CODE = CodeVersion(commit="0" * 40, dirty=True)


@pytest.fixture(scope="module")
def ruleset(warehouse: Engine) -> Iterator[None]:
    with session_scope() as session:
        publish_ruleset(session, published_by="test:ml-pipeline")
    yield
    with warehouse.begin() as conn:
        conn.execute(text("TRUNCATE ml_model_version, ruleset_member, ruleset CASCADE"))


def _train(raw_archive: Path) -> TrainingReport:
    with session_scope() as session:
        return train_denial_risk(
            session,
            archive_root=raw_archive,
            code=CODE,
            trained_by="test:ml-pipeline",
            today=datetime.now(UTC).date(),
            months=SMOKE_MONTHS,
        )


@pytest.fixture(scope="module")
def first(ruleset: None, raw_archive: Path) -> TrainingReport:
    return _train(raw_archive)


def _row(version: int, model_name: str = DENIAL_RISK) -> ModelVersion:
    with session_scope() as session:
        row = session.scalar(
            select(ModelVersion).where(
                ModelVersion.model_name == model_name, ModelVersion.version == version
            )
        )
        assert row is not None
        session.expunge(row)
        return row


def test_ml1_is_trained_judged_and_registered(first: TrainingReport) -> None:
    """FR-ML-03: data hash, code commit, hyperparameters, every number."""
    row = _row(first.version)
    assert row.verdict == str(first.verdict)
    assert row.reason == first.reason
    assert row.data_hash == first.data_hash
    assert len(row.data_hash) == 64
    assert (row.code_commit, row.code_dirty) == (CODE.commit, CODE.dirty)
    assert row.params["objective"] == "binary"
    assert row.params["trees"] >= 1
    assert row.metrics["test"]["pr_auc"] is not None
    assert "rules baseline" in row.baseline
    assert row.windows == first.windows.as_dict()
    assert sum(row.metrics["rows"].values()) > 0


def test_the_data_source_is_read_from_the_files(first: TrainingReport) -> None:
    """The smoke corpus is test traffic (ISA15 = T), so the model is
    SYNTHETIC; nobody passed that in."""
    assert first.data_source is DataSource.SYNTHETIC
    assert _row(first.version).data_source == str(DataSource.SYNTHETIC)


def test_the_registered_model_is_the_one_that_was_measured(first: TrainingReport) -> None:
    model = registry.load(_row(first.version))
    with session_scope() as session:
        rows = denial_lines(session).rows[:50]
    scores = model.predict([r.values for r in rows])
    assert ((scores >= 0) & (scores <= 1)).all()


@pytest.mark.invariant
def test_a_synthetic_model_cannot_load_in_production(
    first: TrainingReport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md section 8: synthetic data never trains a production model."""
    row = _row(first.version)
    monkeypatch.setattr(registry, "get_settings", lambda: SimpleNamespace(environment="staging"))
    registry.load(row)
    monkeypatch.setattr(registry, "get_settings", lambda: SimpleNamespace(environment="production"))
    with pytest.raises(ValidationFailedError) as raised:
        registry.load(row)
    assert "synthetic" in raised.value.context["reason"]


@pytest.mark.invariant
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ml_model_version SET verdict = 'ACCEPTED'",
        "DELETE FROM ml_model_version",
    ],
)
def test_the_model_registry_is_insert_only(
    first: TrainingReport, app_engine: Engine, statement: str
) -> None:
    """A trained model's record is evidence; the app role cannot rewrite it."""
    with pytest.raises(ProgrammingError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(text(statement))


def test_the_same_rows_hash_the_same(first: TrainingReport) -> None:
    with session_scope() as session:
        assert denial_lines(session).data_hash() == first.data_hash


def test_a_second_version_is_judged_against_the_first(
    first: TrainingReport, raw_archive: Path
) -> None:
    """SRS 9.2: beat the current model, not only the rules."""
    second = _train(raw_archive)
    assert second.version == first.version + 1
    assert second.data_hash == first.data_hash
    if first.verdict == "ACCEPTED":
        assert f"v{first.version}" in second.baseline
        # The same rows and code: the same model, which does not beat itself.
        assert second.verdict == "REJECTED"
    assert uuid.UUID(str(_row(second.version).model_version_id))


# ---------------------------------------------------------------------------
# M6.3: the models that decide at the denial
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recovery(ruleset: None, raw_archive: Path) -> TrainingReport:
    with session_scope() as session:
        return train_recovery(
            session,
            archive_root=raw_archive,
            code=CODE,
            trained_by="test:ml-pipeline",
            months=RECOVERY_MONTHS,
        )


@pytest.fixture(scope="module")
def category(ruleset: None, raw_archive: Path) -> TrainingReport:
    with session_scope() as session:
        return train_denial_category(
            session,
            archive_root=raw_archive,
            code=CODE,
            trained_by="test:ml-pipeline",
            months=SMOKE_MONTHS,
        )


def test_ml3_is_judged_against_the_queue_prior(recovery: TrainingReport) -> None:
    row = _row(recovery.version, RECOVERY)
    assert (recovery.task, row.verdict) == (Task.BINARY, str(recovery.verdict))
    assert set(row.baseline) == {"queue prior"}
    assert row.params["objective"] == "binary"
    # A constant prior ranks nothing: its PR-AUC is the share recovered.
    prior = row.baseline["queue prior"]
    assert prior["pr_auc"] == pytest.approx(prior["prevalence"])
    assert recovery.data_source is DataSource.SYNTHETIC


def test_a_recovered_denial_was_paid_later_in_its_chain(
    recovery: TrainingReport, warehouse: Engine
) -> None:
    """ML-3's answer, checked against the mart independently of the reader."""
    with session_scope() as session:
        rows = recovery_lines(session).rows
    recovered = [r for r in rows if r.label]
    assert recovered and len(recovered) < len(rows)
    with warehouse.begin() as conn:
        paid_later: int = conn.execute(
            text(
                "SELECT count(*) FROM analytics_marts.mart_final_outcome d "
                "JOIN analytics_marts.mart_final_outcome l "
                "  ON coalesce(l.root_claim_id, l.claim_id) "
                "   = coalesce(d.root_claim_id, d.claim_id) "
                " AND l.client_id = d.client_id "
                " AND l.submission_attempt > d.submission_attempt "
                " AND l.cpt_hcpcs = d.cpt_hcpcs AND l.line_paid > 0 "
                "WHERE d.service_line_id = :line"
            ),
            {"line": recovered[0].service_line_id},
        ).scalar_one()
    assert paid_later >= 1


def test_ml2_is_judged_against_the_taxonomy_table(category: TrainingReport) -> None:
    """Option (a), ADR-0027: the table is the rules baseline, and ML-2 must
    beat it. The mart's category is the table's answer, so it does not."""
    row = _row(category.version, DENIAL_CATEGORY)
    assert category.task is Task.MULTICLASS
    assert set(row.baseline) == {"taxonomy table"}
    assert row.params["objective"] == "multiclass"
    assert row.params["num_class"] == len(row.encoding["classes"]) >= 2
    table = row.baseline["taxonomy table"]["macro_f1"]
    assert category.metrics["macro_f1"] is not None
    if category.metrics["macro_f1"] <= table:
        assert category.verdict == "REJECTED"
    assert "taxonomy table" in category.reason


def test_ml2_never_learns_from_an_unmapped_denial(category: TrainingReport) -> None:
    with session_scope() as session:
        labels = {r.label for r in category_lines(session).rows}
    assert labels and "UNMAPPED" not in labels


def test_the_registered_ml2_answers_in_categories(category: TrainingReport) -> None:
    model = registry.load(_row(category.version, DENIAL_CATEGORY))
    with session_scope() as session:
        rows = category_lines(session).rows[:50]
    answers = model.predict_class([r.values for r in rows])
    assert set(answers) <= set(model.encoding.classes)
