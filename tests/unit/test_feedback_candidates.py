"""Candidate rules drafted from a denial pattern (M4.9, SRS 8.4, ADR-0013).

A candidate is a rule like any other: it must pass `parse_rule`, it may only
flag, and it is never more than ADVISORY as written. What it can test is
limited to the scrubber's fields, so some categories cannot be a claim rule
at all -- and say so, rather than draft a rule that tests nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rcm.decision import feedback_models
from rcm.decision.denials import DenialCategory
from rcm.decision.feedback import Draft, FeedbackOutcome, NoRule, candidate_for, rule_id_for
from rcm.decision.rules.definition import Layer, Severity, parse_rule

C = DenialCategory
ROOT = Path(__file__).resolve().parents[2]

RULED: dict[DenialCategory, tuple[Layer, str]] = {
    C.REGISTRATION_ELIGIBILITY: (Layer.DEMOGRAPHIC, "ELIGIBILITY_PROBLEM"),
    C.COORDINATION_OF_BENEFITS: (Layer.DEMOGRAPHIC, "ELIGIBILITY_PROBLEM"),
    C.AUTHORISATION: (Layer.PAYER_SPECIFIC, "AUTH_PROBLEM"),
    C.CODING: (Layer.CLINICAL, "SCRUB_EXCEPTION"),
    C.MEDICAL_NECESSITY: (Layer.CLINICAL, "SCRUB_EXCEPTION"),
}


@pytest.mark.parametrize("category", list(RULED))
def test_a_candidate_is_a_valid_advisory_rule(category: DenialCategory) -> None:
    drafted = candidate_for(category, payer_id="00590", cpt="99203", carc="50")
    assert isinstance(drafted, Draft)
    rule = parse_rule(drafted.document)
    layer, route = RULED[category]
    assert rule.layer is layer
    assert rule.action.route_to == route
    # Drafted, never trusted: at most advisory until a person says more.
    assert rule.severity is Severity.ADVISORY
    assert rule.action.auto_correct is False
    assert rule.scope.payer_ids == frozenset({"00590"})
    assert rule.scope.cpts == frozenset({"99203"})
    assert rule.is_line_level
    assert drafted.document["provenance"]["source"] == "denial_feedback"
    assert drafted.document["provenance"]["category"] == str(category)


def test_an_authorisation_candidate_tests_for_the_missing_authorisation() -> None:
    drafted = candidate_for(C.AUTHORISATION, payer_id="00590", cpt="70553", carc="197")
    assert isinstance(drafted, Draft)
    assert {"field": "claim.has_prior_auth", "op": "eq", "value": False} in drafted.document[
        "condition"
    ]["all"]


@pytest.mark.parametrize("category", [C.TIMELY_FILING, C.DUPLICATE, C.PROVIDER_ENROLMENT])
def test_a_category_no_claim_field_can_catch_says_why(category: DenialCategory) -> None:
    """FR-RULES-07: a recorded decision not to create a rule, with its reason."""
    result = candidate_for(category, payer_id="00590", cpt="99203", carc="29")
    assert isinstance(result, NoRule)
    assert len(result.reason) > 20


@pytest.mark.parametrize("category", [C.CONTRACTUAL_NON_COVERED, C.PAYER_ERROR])
def test_a_not_preventable_category_is_never_drafted(category: DenialCategory) -> None:
    with pytest.raises(ValueError, match="not preventable"):
        candidate_for(category, payer_id="00590", cpt="99203", carc="45")


def test_the_outcome_check_matches_the_enum() -> None:
    """The model's CHECK and migration 0023's must spell every outcome."""
    migration = (ROOT / "migrations/versions/0023_denial_feedback_m4_9.py").read_text(
        encoding="utf-8"
    )
    outcomes = {o.value for o in FeedbackOutcome}
    assert set(re.findall(r"'([A-Z_]+)'", feedback_models.OUTCOMES)) == outcomes
    for value in outcomes:
        assert f"'{value}'" in migration, value


def test_one_pattern_one_rule_id() -> None:
    """The same denial pattern finds the same candidate; a payer id with
    characters a rule id refuses still gives a valid one."""
    first = rule_id_for(C.CODING, payer_id="bcbs-tx.01", cpt="99203")
    assert first == rule_id_for(C.CODING, payer_id="bcbs-tx.01", cpt="99203")
    assert first != rule_id_for(C.CODING, payer_id="bcbs_tx_01", cpt="99203")
    assert first != rule_id_for(C.MEDICAL_NECESSITY, payer_id="bcbs-tx.01", cpt="99203")
    drafted = candidate_for(C.CODING, payer_id="bcbs-tx.01", cpt="99203", carc="11")
    assert isinstance(drafted, Draft)
    assert parse_rule(drafted.document).rule_id == first
