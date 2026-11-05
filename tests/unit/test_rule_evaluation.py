"""Evaluating a claim against a ruleset (M4.2, SRS 8.2, ADR-0007).

The pipeline is pure: facts in, findings and a verdict out. Recording the
decision is the caller's (rcm.decision.rules.scrub).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from rcm.decision.rules.definition import LAYER_ORDER, Layer, RuleStatus, parse_rule
from rcm.decision.rules.evaluate import (
    ActiveRule,
    ClaimFacts,
    LineFacts,
    Mode,
    Outcome,
    Verdict,
    evaluate,
)
from rcm.platform.errors import ValidationFailedError


def claim_facts(**overrides: Any) -> dict[str, object]:
    values: dict[str, object] = {
        "claim.payer_id": "12345",
        "claim.claim_frequency_code": "1",
        "claim.claim_filing_code": "CI",
        "claim.place_of_service": "11",
        "claim.total_charge": Decimal("300.00"),
        "claim.date_of_service": date(2026, 3, 2),
        "claim.has_prior_auth": False,
        "claim.diagnoses": ["J069"],
        "claim.procedure_codes": ["99213", "20610"],
        "claim.line_count": 2,
    }
    values.update({f"claim.{k}": v for k, v in overrides.items()})
    return values


def line(number: int, code: str, *modifiers: str, **overrides: Any) -> LineFacts:
    values: dict[str, object] = {
        "line.procedure_code": code,
        "line.modifiers": list(modifiers),
        "line.units": Decimal("1"),
        "line.charge_amount": Decimal("150.00"),
        "line.service_date": date(2026, 3, 2),
    }
    values.update({f"line.{k}": v for k, v in overrides.items()})
    return LineFacts(number=number, values=values)


def facts(*lines: LineFacts, **claim: Any) -> ClaimFacts:
    return ClaimFacts(
        claim=claim_facts(**claim),
        lines=lines or (line(1, "99213"), line(2, "20610")),
    )


def rule(
    condition: dict[str, Any],
    *,
    rule_id: str = "R1",
    layer: str = "PAYER_SPECIFIC",
    severity: str = "BLOCKING",
    status: RuleStatus = RuleStatus.BLOCKING,
    scope: dict[str, Any] | None = None,
) -> ActiveRule:
    parsed = parse_rule(
        {
            "rule_id": rule_id,
            "layer": layer,
            "severity": severity,
            "scope": scope or {},
            "condition": condition,
            "action": {"type": "FLAG", "message": "Review this", "route_to": "SCRUB_EXCEPTION"},
        }
    )
    return ActiveRule(rule=parsed, version=1, status=status)


MOD25_MISSING = {
    "all": [
        {"field": "claim.line_count", "op": "gt", "value": 1},
        {"field": "line.modifiers", "op": "not_contains", "value": "25"},
    ]
}


def test_a_line_rule_flags_the_line_it_finds() -> None:
    result = evaluate([rule(MOD25_MISSING, scope={"cpt": ["99213"]})], facts())
    assert [(f.rule_id, f.line_number, f.outcome) for f in result.findings] == [
        ("R1", 1, Outcome.FLAGGED)
    ]
    assert result.verdict is Verdict.BLOCK


def test_a_line_that_meets_the_rule_is_not_flagged() -> None:
    result = evaluate(
        [rule(MOD25_MISSING, scope={"cpt": ["99213"]})],
        facts(line(1, "99213", "25"), line(2, "20610")),
    )
    assert result.findings == ()
    assert result.verdict is Verdict.PASS


def test_a_rule_scoped_to_another_payer_does_not_see_the_claim() -> None:
    result = evaluate([rule(MOD25_MISSING, scope={"payer_id": ["99999"]})], facts())
    assert result.findings == ()


def test_a_claim_rule_is_checked_once() -> None:
    result = evaluate(
        [rule({"field": "claim.total_charge", "op": "gt", "value": "100.00"})], facts()
    )
    assert [(f.line_number, f.outcome) for f in result.findings] == [(None, Outcome.FLAGGED)]


@pytest.mark.invariant
def test_a_missing_value_is_indeterminate_not_a_pass() -> None:
    """PD-18 / ADR-0007: a rule that cannot be read blocks; it never passes
    for lack of data."""
    result = evaluate(
        [rule({"field": "line.service_date", "op": "lt", "value": "2026-01-01"})],
        facts(line(1, "99213", service_date=None)),
    )
    assert [f.outcome for f in result.findings] == [Outcome.INDETERMINATE]
    assert result.verdict is Verdict.BLOCK


def test_absence_is_asked_about_on_purpose() -> None:
    result = evaluate(
        [rule({"field": "line.service_date", "op": "not_exists"})],
        facts(line(1, "99213", service_date=None)),
    )
    assert [f.outcome for f in result.findings] == [Outcome.FLAGGED]


def test_any_with_one_true_branch_is_true_whatever_the_other() -> None:
    condition = {
        "any": [
            {"field": "line.service_date", "op": "lt", "value": "2026-01-01"},
            {"field": "line.procedure_code", "op": "eq", "value": "99213"},
        ]
    }
    result = evaluate([rule(condition)], facts(line(1, "99213", service_date=None)))
    assert [f.outcome for f in result.findings] == [Outcome.FLAGGED]


def test_all_with_one_false_branch_is_false_whatever_the_other() -> None:
    condition = {
        "all": [
            {"field": "line.service_date", "op": "lt", "value": "2026-01-01"},
            {"field": "line.procedure_code", "op": "eq", "value": "00000"},
        ]
    }
    result = evaluate([rule(condition)], facts(line(1, "99213", service_date=None)))
    assert result.findings == ()


@pytest.mark.parametrize(
    ("severity", "status", "mode", "verdict"),
    [
        ("BLOCKING", RuleStatus.BLOCKING, Mode.BLOCKING, Verdict.BLOCK),
        ("BLOCKING", RuleStatus.ADVISORY, Mode.ADVISORY, Verdict.ADVISE),
        ("ADVISORY", RuleStatus.BLOCKING, Mode.ADVISORY, Verdict.ADVISE),
        ("BLOCKING", RuleStatus.SHADOW, Mode.SHADOW, Verdict.PASS),
        ("INFORMATIONAL", RuleStatus.BLOCKING, Mode.INFORMATIONAL, Verdict.PASS),
    ],
)
def test_a_rule_blocks_only_when_its_severity_and_status_both_say_so(
    severity: str, status: RuleStatus, mode: Mode, verdict: Verdict
) -> None:
    result = evaluate(
        [rule(MOD25_MISSING, severity=severity, status=status, scope={"cpt": ["99213"]})],
        facts(),
    )
    assert [f.mode for f in result.findings] == [mode]
    assert result.verdict is verdict


def test_a_shadow_rule_that_cannot_be_read_does_not_block() -> None:
    result = evaluate(
        [
            rule(
                {"field": "line.service_date", "op": "lt", "value": "2026-01-01"},
                status=RuleStatus.SHADOW,
            )
        ],
        facts(line(1, "99213", service_date=None)),
    )
    assert result.verdict is Verdict.PASS
    assert [f.outcome for f in result.findings] == [Outcome.INDETERMINATE]


@pytest.mark.invariant
def test_layers_run_in_order_and_all_run_after_a_block() -> None:
    """SRS 8.2. A person sees every problem at once, not one per resubmission."""
    temporal = rule(
        {"field": "claim.has_prior_auth", "op": "eq", "value": False},
        rule_id="TEMPORAL_1",
        layer="TEMPORAL",
    )
    structural = rule(
        {"field": "claim.line_count", "op": "gt", "value": 0},
        rule_id="STRUCTURAL_1",
        layer="STRUCTURAL",
    )
    clinical = rule(
        {"field": "line.procedure_code", "op": "eq", "value": "20610"},
        rule_id="CLINICAL_1",
        layer="CLINICAL",
    )
    result = evaluate([temporal, clinical, structural], facts())
    assert result.layers_run == LAYER_ORDER
    assert [f.rule_id for f in result.findings] == ["STRUCTURAL_1", "CLINICAL_1", "TEMPORAL_1"]
    assert [f.layer for f in result.findings] == [
        Layer.STRUCTURAL,
        Layer.CLINICAL,
        Layer.TEMPORAL,
    ]


@pytest.mark.parametrize(
    "status", [RuleStatus.DRAFT, RuleStatus.BACKTESTING, RuleStatus.DEPRECATED]
)
def test_a_rule_that_is_not_live_cannot_be_evaluated(status: RuleStatus) -> None:
    with pytest.raises(ValidationFailedError):
        evaluate([rule(MOD25_MISSING, status=status)], facts())


def test_facts_missing_a_field_are_refused() -> None:
    """A facts builder that forgets a field would make every rule on it
    indeterminate -- or worse, silently skipped. Refused at construction."""
    values = claim_facts()
    del values["claim.has_prior_auth"]
    with pytest.raises(ValidationFailedError):
        ClaimFacts(claim=values, lines=(line(1, "99213"),))


def test_a_value_compares_by_its_type() -> None:
    result = evaluate(
        [rule({"field": "line.units", "op": "gte", "value": "2"})],
        facts(line(1, "99213", units=Decimal("2.0")), line(2, "20610")),
    )
    assert [f.line_number for f in result.findings] == [1]


def test_a_finding_names_its_rule_version_and_route() -> None:
    (finding,) = evaluate([rule(MOD25_MISSING, scope={"cpt": ["99213"]})], facts()).findings
    assert finding.rule_version == 1
    assert finding.route_to == "SCRUB_EXCEPTION"
    assert finding.message == "Review this"
