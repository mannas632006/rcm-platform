"""Training ML-2 and ML-3: what they may read at the denial, and what they
must beat (M6.3, ADR-0027).

The database half -- the readers against a built warehouse, the registry --
is in `tests/leakage/test_ml_pipeline.py`.
"""

from __future__ import annotations

import random
import uuid
from datetime import date

import pytest

from rcm.analytics.dataset import (
    CATEGORY_FEATURES,
    RECOVERY_FEATURES,
    UNMAPPED,
    Feature,
    Kind,
    LineDataset,
    LineRow,
    Value,
)
from rcm.analytics.features import Stage, assert_trainable
from rcm.decision import prioritisation
from rcm.decision.ml.encoding import Encoding
from rcm.decision.ml.evaluate import binary_report, multiclass_report
from rcm.decision.ml.model import Model, fit_multiclass
from rcm.decision.ml.pipeline import Task, _verdict, recovery_prior, taxonomy_answers
from rcm.decision.ml.registry_models import Verdict
from rcm.platform.errors import LeakageError, ValidationFailedError

# ---------------------------------------------------------------------------
# FR-ML-01 at the denial: the 835 is known, nothing after it is
# ---------------------------------------------------------------------------


def test_ml3_reads_the_denial_but_only_at_the_denial() -> None:
    assert_trainable((f.name for f in RECOVERY_FEATURES), stage=Stage.DENIAL)
    # The same inputs, for a model that decides before SEND, read the
    # payer's answer.
    with pytest.raises(LeakageError) as raised:
        assert_trainable(f.name for f in RECOVERY_FEATURES)
    assert raised.value.context["column"] == "carc_code"


def test_ml2_reads_the_refusal_codes_but_not_its_answer() -> None:
    assert_trainable(
        (f.name for f in CATEGORY_FEATURES), stage=Stage.DENIAL, label="denial_category"
    )


@pytest.mark.invariant
@pytest.mark.parametrize("column", ["denial_category", "preventability"])
def test_a_model_never_reads_its_own_answer(column: str) -> None:
    """Preventability is looked up from the category (BR-TAX-01): reading it
    is reading the category."""
    with pytest.raises(LeakageError) as raised:
        assert_trainable(["carc_code", column], stage=Stage.DENIAL, label="denial_category")
    assert raised.value.context["column"] == column
    assert "the answer denial_category" in raised.value.context["reason"]


@pytest.mark.invariant
@pytest.mark.parametrize(
    "column", ["final_outcome", "net_collected", "rework_touches", "is_denied"]
)
def test_what_happened_after_the_denial_is_refused_at_the_denial(column: str) -> None:
    """A resubmission, a payment, the final outcome: ML-3's answers."""
    with pytest.raises(LeakageError) as raised:
        assert_trainable(["carc_code", column], stage=Stage.DENIAL)
    assert raised.value.context["column"] == column
    assert "not knowable at denial" in raised.value.context["reason"]


# ---------------------------------------------------------------------------
# ML-2: a multiclass model
# ---------------------------------------------------------------------------

FEATURES = (
    Feature("payer_id", Kind.CATEGORY),
    Feature("carc_code", Kind.CATEGORY),
    Feature("line_charge", Kind.NUMBER),
)
CATEGORY_BY_CARC = {"197": "AUTHORISATION", "16": "REGISTRATION_ELIGIBILITY", "97": "CODING"}


def _denials(n: int, *, seed: int) -> tuple[list[tuple[Value, ...]], list[str]]:
    rng = random.Random(seed)
    rows: list[tuple[Value, ...]] = []
    labels: list[str] = []
    for _ in range(n):
        carc = rng.choice(sorted(CATEGORY_BY_CARC))
        rows.append((rng.choice(["60054", "00590"]), carc, round(rng.uniform(50, 900), 2)))
        labels.append(CATEGORY_BY_CARC[carc])
    return rows, labels


def _fit_category() -> Model:
    train = _denials(600, seed=1)
    return fit_multiclass(Encoding.fit(FEATURES, train[0]), train, _denials(200, seed=2))


def test_ml2_learns_the_category_and_is_rebuilt_byte_for_byte() -> None:
    model = _fit_category()
    assert model.text() == _fit_category().text()
    assert model.encoding.classes == tuple(sorted(CATEGORY_BY_CARC.values()))
    assert model.predict_class([("60054", "197", 100.0), ("00590", "97", 100.0)]) == [
        "AUTHORISATION",
        "CODING",
    ]
    probabilities = model.predict([("60054", "16", 100.0)])
    assert probabilities.shape == (1, 3)
    assert probabilities.sum() == pytest.approx(1.0)


def test_the_classes_survive_the_registry() -> None:
    encoding = Encoding.fit(FEATURES, _denials(20, seed=3)[0]).with_classes(["CODING", "DUPLICATE"])
    assert Encoding.from_json(encoding.to_json()) == encoding
    # A binary model's encoding is stored exactly as it was before ML-2.
    assert "classes" not in Encoding.fit(FEATURES, []).to_json()


def test_a_class_the_model_never_learned_cannot_be_trained_on() -> None:
    encoding = Encoding.fit(FEATURES, []).with_classes(["CODING"])
    with pytest.raises(ValidationFailedError):
        encoding.class_index(["CODING", "DUPLICATE"])


def test_ml2_drivers_wait_for_m6_4() -> None:
    """Which class's SHAP block to persist is not decided here."""
    with pytest.raises(ValidationFailedError):
        _fit_category().drivers([("60054", "197", 100.0)], top=2)


def test_macro_f1_counts_every_category_the_same() -> None:
    truth = ["CODING"] * 9 + ["DUPLICATE"]
    report = multiclass_report(truth, ["CODING"] * 10, payers=[None] * 10)
    assert report["accuracy"] == pytest.approx(0.9)
    # Right about the common one and never about the rare one: macro-F1 says so.
    assert report["macro_f1"] == pytest.approx((18 / 19 + 0.0) / 2)
    assert report["per_class"]["DUPLICATE"] == {"rows": 1, "f1": 0.0}


def test_unmapped_is_a_miss_not_a_category() -> None:
    report = multiclass_report(["CODING", "CODING"], ["CODING", UNMAPPED], payers=["a", "a"])
    assert report["classes"] == 1
    assert report["accuracy"] == pytest.approx(0.5)
    assert UNMAPPED not in report["per_class"]


def _row[L](values: tuple[Value, ...], label: L) -> LineRow[L]:
    return LineRow(
        service_line_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        payer_id="60054",
        submission_date=date(2025, 1, 1),
        values=values,
        label=label,
    )


def test_the_taxonomy_baseline_reads_the_carc_as_the_platform_does() -> None:
    dataset: LineDataset[str] = LineDataset(FEATURES, ())
    rows = [
        _row(("60054", "197", 1.0), "AUTHORISATION"),
        _row(("60054", "999", 1.0), "CODING"),
        _row(("60054", None, 1.0), "CODING"),
    ]
    assert taxonomy_answers(dataset, rows) == ["AUTHORISATION", UNMAPPED, UNMAPPED]


@pytest.mark.invariant
def test_ml2_must_beat_the_taxonomy_table() -> None:
    """A tie with the table is not a win: the table gives that answer free."""
    tied = _verdict(
        {"macro_f1": 1.0}, {"taxonomy table": {"macro_f1": 1.0}}, lift=0.0, task=Task.MULTICLASS
    )
    assert tied == (Verdict.REJECTED, "macro-F1 1.0000; does not beat taxonomy table 1.0000")
    better, _ = _verdict(
        {"macro_f1": 0.9}, {"taxonomy table": {"macro_f1": 0.8}}, lift=0.0, task=Task.MULTICLASS
    )
    assert better is Verdict.ACCEPTED


# ---------------------------------------------------------------------------
# ML-3: against what the queue believes today
# ---------------------------------------------------------------------------


def test_the_queue_prior_is_the_one_the_queue_ranks_by() -> None:
    rows = [_row((), True), _row((), False), _row((), False), _row((), False)]
    prior = recovery_prior(rows)
    assert prior == [prioritisation.P_RECOVERY["DENIAL"]] * 4
    # A constant ranks nothing: its PR-AUC is the share recovered.
    report = binary_report([r.label for r in rows], prior, payers=[None] * 4)
    assert report["pr_auc"] == pytest.approx(0.25)
