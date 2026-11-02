"""Rules are data, checked when written, not when run (M4.1, ADR-0007).

SRS 8.1: "Declarative, versioned, stored as data -- not code deployments."
A rule that names a field the scrubber does not know, or compares a date to
a word, is refused by name before it can ever evaluate a claim.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from rcm.decision.rules.definition import (
    FIELDS,
    LAYER_ORDER,
    AllOf,
    AnyOf,
    Layer,
    Leaf,
    Not,
    Op,
    Severity,
    parse_rule,
)
from rcm.platform.errors import ValidationFailedError


def doc(**overrides: Any) -> dict[str, Any]:
    """SRS 8.1's example, as a document an author would write."""
    values: dict[str, Any] = {
        "rule_id": "PAYER_X_MOD25_REQUIRED",
        "layer": "PAYER_SPECIFIC",
        "severity": "BLOCKING",
        "scope": {"payer_id": ["12345"], "cpt": ["99213", "99214"]},
        "condition": {
            "all": [
                {"field": "claim.line_count", "op": "gt", "value": 1},
                {"field": "line.modifiers", "op": "not_contains", "value": "25"},
            ]
        },
        "action": {
            "type": "FLAG",
            "auto_correct": False,
            "message": "E/M with same-day procedure requires modifier 25 review for this payer",
            "route_to": "SCRUB_EXCEPTION",
        },
        "provenance": {"source": "DENIAL_ANALYSIS", "authored_by": "ar_lead"},
    }
    values.update(overrides)
    return values


def refused(document: dict[str, Any]) -> ValidationFailedError:
    with pytest.raises(ValidationFailedError) as caught:
        parse_rule(document)
    return caught.value


def test_the_srs_example_parses() -> None:
    rule = parse_rule(doc())
    assert rule.rule_id == "PAYER_X_MOD25_REQUIRED"
    assert rule.layer is Layer.PAYER_SPECIFIC
    assert rule.severity is Severity.BLOCKING
    assert rule.scope.payer_ids == frozenset({"12345"})
    assert rule.scope.cpts == frozenset({"99213", "99214"})
    assert rule.condition == AllOf(
        (
            Leaf("claim.line_count", Op.GT, 1),
            Leaf("line.modifiers", Op.NOT_CONTAINS, "25"),
        )
    )
    assert rule.is_line_level is True


def test_a_rule_naming_no_line_field_is_checked_once_per_claim() -> None:
    rule = parse_rule(doc(condition={"field": "claim.has_prior_auth", "op": "eq", "value": False}))
    assert rule.is_line_level is False


def test_the_layer_order_is_fixed() -> None:
    """SRS 8.2: the order is part of the rule, not a preference."""
    assert LAYER_ORDER == (
        Layer.STRUCTURAL,
        Layer.DEMOGRAPHIC,
        Layer.CLINICAL,
        Layer.PAYER_SPECIFIC,
        Layer.TEMPORAL,
        Layer.LEARNED,
    )


def test_nested_conditions_parse() -> None:
    rule = parse_rule(
        doc(
            condition={
                "any": [
                    {"not": {"field": "line.units", "op": "lte", "value": "4"}},
                    {"field": "line.procedure_code", "op": "in", "value": ["97110", "97140"]},
                ]
            }
        )
    )
    assert rule.condition == AnyOf(
        (
            Not(Leaf("line.units", Op.LTE, Decimal("4"))),
            Leaf("line.procedure_code", Op.IN, ("97110", "97140")),
        )
    )


def test_values_take_the_fields_type() -> None:
    rule = parse_rule(
        doc(
            condition={
                "all": [
                    {"field": "claim.total_charge", "op": "gte", "value": "1000.00"},
                    {"field": "claim.date_of_service", "op": "lt", "value": "2026-01-01"},
                ]
            }
        )
    )
    assert isinstance(rule.condition, AllOf)
    money, when = rule.condition.items
    assert money == Leaf("claim.total_charge", Op.GTE, Decimal("1000.00"))
    assert when == Leaf("claim.date_of_service", Op.LT, date(2026, 1, 1))


@pytest.mark.parametrize(
    ("condition", "where"),
    [
        ({"field": "claim.member_id", "op": "eq", "value": "x"}, "field"),
        ({"field": "claim.payer_id", "op": "matches", "value": ".*"}, "op"),
        ({"field": "claim.date_of_service", "op": "contains", "value": "2026"}, "op"),
        ({"field": "claim.date_of_service", "op": "gt", "value": "soon"}, "value"),
        ({"field": "claim.total_charge", "op": "gt", "value": 10.5}, "value"),
        ({"field": "claim.line_count", "op": "eq", "value": True}, "value"),
        ({"field": "claim.payer_id", "op": "in", "value": []}, "value"),
        ({"field": "claim.payer_id", "op": "in", "value": "12345"}, "value"),
        ({"field": "claim.has_prior_auth", "op": "gt", "value": True}, "op"),
        ({"field": "claim.payer_id", "op": "exists", "value": "x"}, "value"),
        ({"all": []}, "condition"),
        ({"all": [], "any": []}, "condition"),
        ({"field": "claim.payer_id", "op": "eq"}, "value"),
    ],
)
def test_a_bad_condition_is_refused_by_name(condition: dict[str, Any], where: str) -> None:
    error = refused(doc(condition=condition))
    assert where in error.context["field"]


def test_no_field_carries_phi() -> None:
    """PD-13: a finding's inputs are recorded in full, so none may be PHI."""
    for name in FIELDS:
        assert not any(
            word in name for word in ("member", "name", "birth", "dob", "ssn", "address")
        ), name


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("layer", "WHATEVER"),
        ("severity", "CRITICAL"),
        ("rule_id", ""),
        ("rule_id", "has spaces"),
        ("scope", {"payer_id": "12345"}),
        ("scope", {"client": ["a"]}),
        ("action", {"type": "FLAG", "message": "x", "route_to": "NOWHERE"}),
        ("action", {"type": "FIX", "message": "x", "route_to": "SCRUB_EXCEPTION"}),
        ("action", {"type": "FLAG", "message": "", "route_to": "SCRUB_EXCEPTION"}),
    ],
)
def test_a_bad_rule_is_refused(key: str, value: Any) -> None:
    refused(doc(**{key: value}))


def test_auto_correction_is_refused_until_the_fixes_exist() -> None:
    """ADR-0007: nothing changes a claim before M4.7's PD-16 refusals do."""
    error = refused(
        doc(
            action={
                "type": "FLAG",
                "auto_correct": True,
                "message": "Add modifier 25",
                "route_to": "SCRUB_EXCEPTION",
            }
        )
    )
    assert "auto_correct" in error.context["field"]


def test_the_learned_layer_takes_no_hand_written_rule() -> None:
    """Layer 6 is the model's (M6). A hand-written rule there would be read
    as the model's output."""
    refused(doc(layer="LEARNED"))


def test_a_message_carrying_a_placeholder_is_refused() -> None:
    """Messages are fixed text: nothing from the claim is spliced in, so no
    PHI can reach a finding by way of a template."""
    refused(
        doc(
            action={
                "type": "FLAG",
                "message": "Member {claim.member_id} needs review",
                "route_to": "SCRUB_EXCEPTION",
            }
        )
    )
