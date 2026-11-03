"""The denial taxonomy (M4.8, DOC-02 8.1, ADR-0012).

BR-TAX-01: "Every denial MUST map to exactly one category and one
preventability determination. The CARC->category mapping MUST be
version-controlled."
"""

from __future__ import annotations

import csv
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from rcm.analytics.denials import COST_SHARE, PAYMENT_ADJUSTMENT, is_refusal
from rcm.decision import denial_models
from rcm.decision.denials import (
    CARC_CATEGORY,
    CATEGORY_RULES,
    Classification,
    DenialCategory,
    Preventability,
    Reason,
    Unmapped,
    classify_line,
)

ROOT = Path(__file__).resolve().parents[2]
C = DenialCategory
P = Preventability


def reason(
    group: str, carc: str, amount: str = "100.00", sequence: int = 1, adjustment: int = 0
) -> Reason:
    return Reason(
        group=group,
        carc=carc,
        amount=Decimal(amount),
        sequence=sequence,
        adjustment_id=uuid.UUID(int=adjustment or sequence),
    )


def test_the_ten_categories_are_doc_02s() -> None:
    assert [c.value for c in C] == [
        "REGISTRATION_ELIGIBILITY",
        "AUTHORISATION",
        "CODING",
        "MEDICAL_NECESSITY",
        "TIMELY_FILING",
        "DUPLICATE",
        "COORDINATION_OF_BENEFITS",
        "CONTRACTUAL_NON_COVERED",
        "PROVIDER_ENROLMENT",
        "PAYER_ERROR",
    ]


def test_each_category_carries_doc_02s_preventability() -> None:
    expected = {
        C.REGISTRATION_ELIGIBILITY: P.PREVENTABLE,
        C.AUTHORISATION: P.PREVENTABLE,
        C.CODING: P.PREVENTABLE,
        C.MEDICAL_NECESSITY: P.PARTIALLY_PREVENTABLE,
        C.TIMELY_FILING: P.PREVENTABLE,
        C.DUPLICATE: P.PREVENTABLE,
        C.COORDINATION_OF_BENEFITS: P.PREVENTABLE,
        C.CONTRACTUAL_NON_COVERED: P.NOT_PREVENTABLE,
        C.PROVIDER_ENROLMENT: P.PREVENTABLE,
        C.PAYER_ERROR: P.NOT_PREVENTABLE,
    }
    assert {c: rule.preventability for c, rule in CATEGORY_RULES.items()} == expected


@pytest.mark.invariant
def test_every_refusal_code_the_payer_can_send_maps_to_one_category() -> None:
    """BR-TAX-01 over every CARC the synthetic adjudicator can send. A code
    left out would be a denial nobody classified."""
    with (ROOT / "tools/synthetic/reference/carc.csv").open(encoding="utf-8") as handle:
        sent = {(row["usual_group"], row["carc_code"]) for row in csv.DictReader(handle)}
    refusals = {carc for group, carc in sent if is_refusal(group, carc)}
    assert refusals
    assert refusals <= set(CARC_CATEGORY)


@pytest.mark.parametrize("carc", ["1", "2", "3"])
def test_patient_cost_share_is_not_a_refusal(carc: str) -> None:
    assert ("PR", carc) in COST_SHARE
    assert not is_refusal("PR", carc)
    assert classify_line([reason("PR", carc)]) is None


def test_pr_204_is_a_refusal_whoever_is_billed() -> None:
    """ADR-0012: group PR says who may be billed, not whether the payer
    refused. 'Not covered under the plan' is a refusal."""
    assert is_refusal("PR", "204")
    result = classify_line([reason("PR", "204")])
    assert isinstance(result, Classification)
    assert result.category is C.CONTRACTUAL_NON_COVERED
    assert result.preventability is P.NOT_PREVENTABLE


def test_a_co_reason_on_a_cost_share_carc_is_still_a_refusal() -> None:
    """Cost share is the pair, not the number: CO-1 is not a deductible."""
    assert is_refusal("CO", "1")


@pytest.mark.invariant
def test_a_line_with_several_reasons_gets_exactly_one_category() -> None:
    """EC-62: the largest refusal decides; every reason is kept."""
    result = classify_line(
        [
            reason("CO", "50", "40.00", 1),
            reason("CO", "197", "160.00", 2),
            reason("PR", "1", "500.00", 3),
        ]
    )
    assert isinstance(result, Classification)
    assert result.category is C.AUTHORISATION
    assert result.carc == "197"
    assert [r.carc for r in result.considered] == ["50", "197", "1"]


def test_a_tie_goes_to_the_earliest_reason() -> None:
    result = classify_line([reason("CO", "50", "100.00", 2), reason("CO", "29", "100.00", 1)])
    assert isinstance(result, Classification)
    assert result.category is C.TIMELY_FILING


@pytest.mark.invariant
def test_an_unknown_refusal_code_is_never_guessed() -> None:
    """PD-18: reported as unmapped, never filed under a likely category."""
    result = classify_line([reason("CO", "A1")])
    assert isinstance(result, Unmapped)
    assert result.carcs == ("A1",)


def test_payer_error_is_assigned_by_a_person_not_a_code() -> None:
    assert C.PAYER_ERROR not in CARC_CATEGORY.values()


def test_the_warehouse_reads_the_same_table() -> None:
    """ADR-0012: one mapping. The dbt model's rows must equal this module's."""
    sql = (ROOT / "src/rcm/analytics/dbt/models/core/core_denial_taxonomy.sql").read_text(
        encoding="utf-8"
    )
    rows = set(re.findall(r"\('([0-9A-Z]+)',\s*'([A-Z_]+)',\s*'([A-Z_]+)'\)", sql))
    expected = {
        (carc, str(category), str(CATEGORY_RULES[category].preventability))
        for carc, category in CARC_CATEGORY.items()
    }
    assert rows == expected


def test_the_warehouse_reads_the_same_cost_share() -> None:
    """The denial rule's exceptions, spelled the same in both dbt models."""
    pair = "not (a.cas_group_code = 'PR' and a.carc_code in ('1', '2', '3'))"
    adjustment = "a.carc_code not in ('23', '24', '45')"
    assert sorted(COST_SHARE) == [("PR", "1"), ("PR", "2"), ("PR", "3")]
    assert sorted(PAYMENT_ADJUSTMENT) == ["23", "24", "45"]
    for model in ("core/core_trailing_denial_rates.sql", "marts/mart_claim_line_outcome.sql"):
        text = (ROOT / "src/rcm/analytics/dbt/models" / model).read_text(encoding="utf-8")
        assert pair in text, model
        assert adjustment in text, model


@pytest.mark.invariant
def test_co_45_beside_a_deductible_is_not_a_denial() -> None:
    """ADR-0012: the deductible swallowed the allowed amount and the fee
    schedule cut the rest. Real 835s send this every January; it is not a
    refusal, and calling it one fills the denial queue with paid claims."""
    assert classify_line([reason("CO", "45", "40.00", 1), reason("PR", "1", "60.00", 2)]) is None


@pytest.mark.parametrize(("group", "carc"), [("OA", "23"), ("CO", "24"), ("CO", "45")])
def test_a_payment_adjustment_is_not_a_refusal(group: str, carc: str) -> None:
    """A secondary paying nothing after the primary (OA-23) and capitation
    (CO-24) are the correct outcome, not a refusal."""
    assert not is_refusal(group, carc)
    assert classify_line([reason(group, carc)]) is None


def test_a_payment_adjustment_never_decides_the_category() -> None:
    """A larger CO-45 must not hide a preventable CO-50 from the feedback loop."""
    result = classify_line([reason("CO", "45", "70.00", 1), reason("CO", "50", "30.00", 2)])
    assert isinstance(result, Classification)
    assert result.category is C.MEDICAL_NECESSITY


def test_a_full_tie_goes_to_the_adjustment_id() -> None:
    """CAS sequence is not unique on a line. The mart orders the same way."""
    first = reason("CO", "50", "100.00", 1, adjustment=2)
    second = reason("CO", "29", "100.00", 1, adjustment=1)
    for order in ([first, second], [second, first]):
        result = classify_line(order)
        assert isinstance(result, Classification)
        assert result.category is C.TIMELY_FILING


def test_an_unmapped_largest_refusal_is_not_overridden_by_a_smaller_one() -> None:
    """The category comes from the reason that cost the most, or a person."""
    result = classify_line([reason("CO", "A1", "150.00", 1), reason("CO", "50", "50.00", 2)])
    assert isinstance(result, Unmapped)
    assert result.carcs == ("50", "A1")


def test_the_category_check_matches_the_enum() -> None:
    """The model's CHECK and migration 0021's must spell every category, or a
    new one is refused at insert time."""
    migration = (ROOT / "migrations/versions/0021_denial_classification_m4_8.py").read_text(
        encoding="utf-8"
    )
    categories = {c.value for c in C}
    preventability = {p.value for p in P}
    assert set(re.findall(r"'([A-Z_]+)'", denial_models.CATEGORIES)) == categories
    assert set(re.findall(r"'([A-Z_]+)'", denial_models.PREVENTABILITY)) == preventability
    for value in categories | preventability:
        assert f"'{value}'" in migration, value
