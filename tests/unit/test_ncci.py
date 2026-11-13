"""NCCI edits: loaded whole, checked in Layer 3 (M4.3, ADR-0008).

EC-37: an NCCI edit with a valid modifier bypass passes. PD-16: the system
never adds the modifier; it asks a person.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from rcm.decision.ncci import MueLimit, NcciCheck, PtpEdit, parse_mue, parse_ptp
from rcm.decision.rules.definition import Layer
from rcm.decision.rules.evaluate import LineFacts, Mode, Outcome, Verdict, evaluate
from rcm.platform.errors import ValidationFailedError
from tests.unit.test_rule_evaluation import facts, line, rule

PTP_CSV = (
    b"column_1,column_2,effective_date,deletion_date,modifier_indicator,rationale\n"
    b"45385,45380,19960101,*,1,Standards of medical practice\n"
    b"45380,45378,20020101,20250101,0,Misuse of column two code\n"
    b"99213,36415,19960101,,9,Not applicable\n"
)
MUE_CSV = b"code,mue_value,adjudication_indicator,rationale\n97110,4,2,Clinical data\n20610,2,1,\n"


def ptp(c1: str, c2: str, indicator: str = "1", *, deleted: date | None = None) -> PtpEdit:
    return PtpEdit(c1, c2, date(1996, 1, 1), deleted, indicator)


def check(*edits: PtpEdit, mue: tuple[MueLimit, ...] = ()) -> NcciCheck:
    return NcciCheck(ptp_release="PTP:TEST", mue_release="MUE:TEST", ptp=edits, mue=mue)


def ncci_findings(c: NcciCheck, *lines: LineFacts) -> list[tuple[str, int | None, Outcome]]:
    result = evaluate([], facts(*lines), checks=[c])
    return [(f.rule_id, f.line_number, f.outcome) for f in result.findings]


# ---------------------------------------------------------------------------
# Loading: whole, or not at all
# ---------------------------------------------------------------------------


def test_a_ptp_file_parses() -> None:
    edits = parse_ptp(PTP_CSV)
    assert edits == [
        PtpEdit("45385", "45380", date(1996, 1, 1), None, "1"),
        PtpEdit("45380", "45378", date(2002, 1, 1), date(2025, 1, 1), "0"),
        PtpEdit("99213", "36415", date(1996, 1, 1), None, "9"),
    ]


def test_an_mue_file_parses() -> None:
    assert parse_mue(MUE_CSV) == [
        MueLimit("97110", Decimal("4"), "2"),
        MueLimit("20610", Decimal("2"), "1"),
    ]


@pytest.mark.parametrize(
    "row",
    [
        b"45385,45380,19960101,*,7,x",  # unknown modifier indicator
        b"45385,,19960101,*,1,x",  # missing column 2
        b"45385,45380,1996-01-01,*,1,x",  # wrong date format
        b"45385,45380,20250101,20240101,1,x",  # deleted before it began
        b"4538X5,45380,19960101,*,1,x",  # not a code
    ],
)
def test_one_bad_row_refuses_the_whole_file(row: bytes) -> None:
    payload = PTP_CSV + row + b"\n"
    with pytest.raises(ValidationFailedError) as caught:
        parse_ptp(payload)
    assert "row 5" in caught.value.context["field"]


def test_a_file_with_the_wrong_columns_is_refused() -> None:
    with pytest.raises(ValidationFailedError):
        parse_ptp(MUE_CSV)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(ValidationFailedError):
        parse_ptp(b"column_1,column_2,effective_date,deletion_date,modifier_indicator\n")


@pytest.mark.parametrize("value", [b"0", b"-1", b"two"])
def test_an_mue_limit_must_be_a_positive_number(value: bytes) -> None:
    with pytest.raises(ValidationFailedError):
        parse_mue(b"code,mue_value,adjudication_indicator\n97110," + value + b",1\n")


# ---------------------------------------------------------------------------
# PTP
# ---------------------------------------------------------------------------


def test_a_bundled_pair_flags_the_column_two_line() -> None:
    found = ncci_findings(check(ptp("45385", "45380", "0")), line(1, "45385"), line(2, "45380"))
    assert found == [("NCCI_PTP", 2, Outcome.FLAGGED)]


@pytest.mark.invariant
def test_a_valid_modifier_bypass_passes() -> None:
    """EC-37."""
    found = ncci_findings(
        check(ptp("45385", "45380", "1")), line(1, "45385"), line(2, "45380", "59")
    )
    assert found == []


def test_a_bypass_is_no_use_when_the_edit_allows_none() -> None:
    found = ncci_findings(
        check(ptp("45385", "45380", "0")), line(1, "45385"), line(2, "45380", "59")
    )
    assert found == [("NCCI_PTP", 2, Outcome.FLAGGED)]


def test_a_modifier_on_the_column_one_line_does_not_bypass() -> None:
    found = ncci_findings(
        check(ptp("45385", "45380", "1")), line(1, "45385", "59"), line(2, "45380")
    )
    assert found == [("NCCI_PTP", 2, Outcome.FLAGGED)]


def test_an_edit_marked_not_applicable_never_fires() -> None:
    found = ncci_findings(check(ptp("45385", "45380", "9")), line(1, "45385"), line(2, "45380"))
    assert found == []


def test_codes_on_different_dates_are_not_a_pair() -> None:
    found = ncci_findings(
        check(ptp("45385", "45380", "0")),
        line(1, "45385", service_date=date(2026, 3, 2)),
        line(2, "45380", service_date=date(2026, 3, 9)),
    )
    assert found == []


def test_a_deleted_edit_does_not_apply_on_or_after_its_deletion() -> None:
    edit = ptp("45385", "45380", "0", deleted=date(2026, 3, 2))
    assert ncci_findings(check(edit), line(1, "45385"), line(2, "45380")) == []
    earlier = date(2026, 3, 1)
    assert ncci_findings(
        check(edit),
        line(1, "45385", service_date=earlier),
        line(2, "45380", service_date=earlier),
    ) == [("NCCI_PTP", 2, Outcome.FLAGGED)]


def test_an_edit_does_not_apply_before_it_takes_effect() -> None:
    edit = PtpEdit("45385", "45380", date(2027, 1, 1), None, "0")
    assert ncci_findings(check(edit), line(1, "45385"), line(2, "45380")) == []


def test_a_line_with_no_date_anywhere_is_indeterminate() -> None:
    result = evaluate(
        [],
        facts(
            line(1, "45385", service_date=None),
            line(2, "45380", service_date=None),
            date_of_service=None,
        ),
        checks=[check(ptp("45385", "45380", "0"))],
    )
    assert [(f.line_number, f.outcome) for f in result.findings] == [(2, Outcome.INDETERMINATE)]
    assert result.verdict is Verdict.BLOCK


def test_a_line_falls_back_to_the_claims_date_of_service() -> None:
    found = ncci_findings(
        check(ptp("45385", "45380", "0")),
        line(1, "45385", service_date=None),
        line(2, "45380", service_date=None),
    )
    assert found == [("NCCI_PTP", 2, Outcome.FLAGGED)]


def test_a_finding_names_the_edit_and_blocks() -> None:
    result = evaluate(
        [],
        facts(line(1, "45385"), line(2, "45380")),
        checks=[check(ptp("45385", "45380", "1"))],
    )
    (finding,) = result.findings
    assert finding.layer is Layer.CLINICAL
    assert finding.mode is Mode.BLOCKING
    assert finding.reference == "ncci-ptp PTP:TEST 45385/45380 mi=1"
    assert finding.route_to == "SCRUB_EXCEPTION"
    assert result.verdict is Verdict.BLOCK


@pytest.mark.invariant
def test_ncci_never_adds_the_bypass_modifier() -> None:
    """PD-16: a finding asks a person. The facts are untouched."""
    claim = facts(line(1, "45385"), line(2, "45380"))
    before = [dict(item.values) for item in claim.lines]
    evaluate([], claim, checks=[check(ptp("45385", "45380", "1"))])
    assert [dict(item.values) for item in claim.lines] == before


# ---------------------------------------------------------------------------
# MUE
# ---------------------------------------------------------------------------


def test_a_line_over_its_mue_is_flagged() -> None:
    limit = MueLimit("20610", Decimal("2"), "1")
    found = ncci_findings(
        check(mue=(limit,)), line(1, "20610", units=Decimal("3")), line(2, "99213")
    )
    assert found == [("NCCI_MUE", 1, Outcome.FLAGGED)]


def test_a_per_day_mue_sums_the_lines_of_that_day() -> None:
    limit = MueLimit("97110", Decimal("4"), "2")
    found = ncci_findings(
        check(mue=(limit,)),
        line(1, "97110", units=Decimal("3")),
        line(2, "97110", units=Decimal("2")),
    )
    assert found == [("NCCI_MUE", 1, Outcome.FLAGGED)]


def test_a_per_line_mue_does_not_sum() -> None:
    limit = MueLimit("20610", Decimal("2"), "1")
    found = ncci_findings(
        check(mue=(limit,)),
        line(1, "20610", units=Decimal("2")),
        line(2, "20610", units=Decimal("2")),
    )
    assert found == []


@pytest.mark.parametrize("modifier", ["76", "77"])
def test_a_repeat_procedure_on_its_own_line_is_not_flagged(modifier: str) -> None:
    """EC-38: the same procedure twice on one day, the repeat on its own line
    with 76 (same physician) or 77 (another). A line-edit MUE reads each line
    alone, and a code is never a PTP pair with itself."""
    limit = MueLimit("71045", Decimal("1"), "1")
    found = ncci_findings(
        check(ptp("71046", "71045", "0"), mue=(limit,)),
        line(1, "71045"),
        line(2, "71045", modifier),
    )
    assert found == []


def test_a_repeat_over_a_per_day_mue_goes_to_a_person() -> None:
    """EC-38, the other half: a date-of-service MUE counts the day's units
    whatever the modifier, so a repeat past it is put to a person. Nothing
    removes the line or the modifier."""
    limit = MueLimit("71045", Decimal("1"), "3")
    found = ncci_findings(check(mue=(limit,)), line(1, "71045"), line(2, "71045", "76"))
    assert found == [("NCCI_MUE", 1, Outcome.FLAGGED)]


def test_layer_three_runs_rules_then_ncci_in_order() -> None:
    clinical = rule(
        {"field": "line.procedure_code", "op": "eq", "value": "45385"},
        rule_id="CLINICAL_RULE",
        layer="CLINICAL",
    )
    structural = rule(
        {"field": "claim.line_count", "op": "gt", "value": 0},
        rule_id="STRUCTURAL_RULE",
        layer="STRUCTURAL",
    )
    temporal = rule(
        {"field": "claim.has_prior_auth", "op": "eq", "value": False},
        rule_id="TEMPORAL_RULE",
        layer="TEMPORAL",
    )
    result = evaluate(
        [temporal, clinical, structural],
        facts(line(1, "45385"), line(2, "45380")),
        checks=[check(ptp("45385", "45380", "0"))],
    )
    assert [f.rule_id for f in result.findings] == [
        "STRUCTURAL_RULE",
        "CLINICAL_RULE",
        "NCCI_PTP",
        "TEMPORAL_RULE",
    ]
