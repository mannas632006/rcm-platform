"""Auto-correction: deterministic fixes only, never a modifier (M4.7, ADR-0011).

BR-M7-03: auto-correction only for deterministic fixes.
PD-16 / FR-RULES-05: modifiers 25 and 59 are never auto-applied.
"""

from __future__ import annotations

from typing import Any

import pytest

from rcm.decision.rules import correct
from rcm.decision.rules.definition import RuleStatus, parse_rule
from rcm.decision.rules.evaluate import ActiveRule, ClaimFacts, Verdict
from rcm.platform.errors import ManualCodingRequiredError, ValidationFailedError
from tests.unit.test_rule_evaluation import facts, line


def rule_doc(fix: str | None, condition: dict[str, Any], **action: Any) -> dict[str, Any]:
    act: dict[str, Any] = {
        "type": "FLAG",
        "message": "Fix the format",
        "route_to": "SCRUB_EXCEPTION",
    }
    if fix is not None:
        act.update(auto_correct=True, fix=fix)
    act.update(action)
    return {
        "rule_id": "FORMAT_RULE",
        "layer": "STRUCTURAL",
        "severity": "BLOCKING",
        "condition": condition,
        "action": act,
    }


LOWER_RT = {"field": "line.modifiers", "op": "contains", "value": "rt"}
DOTTED = {"field": "claim.diagnoses", "op": "contains", "value": "J06.9"}


def active(document: dict[str, Any], status: RuleStatus = RuleStatus.BLOCKING) -> ActiveRule:
    return ActiveRule(rule=parse_rule(document), version=1, status=status)


def modifiers(claim: ClaimFacts, number: int) -> list[str]:
    (found,) = [item for item in claim.lines if item.number == number]
    value = found.values["line.modifiers"]
    assert isinstance(value, list)
    return value


# ---------------------------------------------------------------------------
# What a rule may ask for
# ---------------------------------------------------------------------------


def test_a_catalogued_fix_is_accepted() -> None:
    assert parse_rule(rule_doc("UPPERCASE_MODIFIERS", LOWER_RT)).action.fix == (
        "UPPERCASE_MODIFIERS"
    )
    assert parse_rule(rule_doc("STRIP_DIAGNOSIS_DECIMAL", DOTTED)).action.fix == (
        "STRIP_DIAGNOSIS_DECIMAL"
    )


def test_auto_correct_without_a_fix_is_refused() -> None:
    with pytest.raises(ValidationFailedError):
        parse_rule(rule_doc(None, LOWER_RT, auto_correct=True))


def test_an_unknown_fix_is_refused() -> None:
    with pytest.raises(ValidationFailedError):
        parse_rule(rule_doc("REWRITE_CPT", LOWER_RT))


def test_a_line_fix_needs_a_line_rule() -> None:
    with pytest.raises(ValidationFailedError):
        parse_rule(rule_doc("UPPERCASE_MODIFIERS", DOTTED))


@pytest.mark.invariant
def test_a_rule_cannot_auto_apply_modifier_25() -> None:
    """PD-16, refused by name when the rule is written."""
    with pytest.raises(ManualCodingRequiredError):
        parse_rule(rule_doc("ADD_MODIFIER", LOWER_RT, value="25"))


@pytest.mark.invariant
def test_a_rule_cannot_auto_apply_modifier_59() -> None:
    with pytest.raises(ManualCodingRequiredError):
        parse_rule(rule_doc("ADD_MODIFIER", LOWER_RT, value="59"))


@pytest.mark.parametrize("value", ["XE", "XP", "XS", "XU"])
def test_the_59_subset_is_refused_the_same_way(value: str) -> None:
    with pytest.raises(ManualCodingRequiredError):
        parse_rule(rule_doc("ADD_MODIFIER", LOWER_RT, value=value))


def test_no_modifier_is_ever_added_automatically() -> None:
    """Choosing a modifier is a coding judgment, whichever one it is."""
    with pytest.raises(ValidationFailedError):
        parse_rule(rule_doc("ADD_MODIFIER", LOWER_RT, value="RT"))


# ---------------------------------------------------------------------------
# The fixes
# ---------------------------------------------------------------------------


def test_uppercase_modifiers() -> None:
    fixed, change = correct.apply_fix(facts(line(1, "99213", "rt", "lt")), "UPPERCASE_MODIFIERS", 1)
    assert modifiers(fixed, 1) == ["RT", "LT"]
    assert change is not None
    assert (change.field, change.line_number, change.before, change.after) == (
        "line.modifiers",
        1,
        ["rt", "lt"],
        ["RT", "LT"],
    )


def test_deduplicate_modifiers_keeps_order() -> None:
    fixed, _ = correct.apply_fix(
        facts(line(1, "99213", "RT", "LT", "RT")), "DEDUPLICATE_MODIFIERS", 1
    )
    assert modifiers(fixed, 1) == ["RT", "LT"]


def test_deduplicating_modifiers_never_merges_a_repeat_procedure() -> None:
    """EC-38: two lines for one procedure are a repeat service, not a
    duplicate to tidy. The fix works inside one line; both lines and the 76
    stay as billed."""
    billed = facts(line(1, "71045"), line(2, "71045", "76"))
    fixed, change = correct.apply_fix(billed, "DEDUPLICATE_MODIFIERS", 2)
    assert change is None
    assert [(ln.number, ln.values["line.procedure_code"]) for ln in fixed.lines] == [
        (1, "71045"),
        (2, "71045"),
    ]
    assert modifiers(fixed, 2) == ["76"]


def test_strip_diagnosis_decimal() -> None:
    fixed, change = correct.apply_fix(
        facts(diagnoses=["J06.9", "E11.65"]), "STRIP_DIAGNOSIS_DECIMAL", None
    )
    assert fixed.claim["claim.diagnoses"] == ["J069", "E1165"]
    assert change is not None
    assert change.line_number is None


def test_a_fix_that_changes_nothing_says_so() -> None:
    _, change = correct.apply_fix(facts(line(1, "99213", "RT")), "UPPERCASE_MODIFIERS", 1)
    assert change is None


@pytest.mark.invariant
def test_the_corrector_refuses_any_fix_that_adds_modifier_25(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0011 Decision 2: the second line of defence. A rogue fix that
    slips past the catalogue is still refused when it runs."""

    def rogue(values: list[str]) -> list[str]:
        return [*values, "25"]

    monkeypatch.setitem(correct.LINE_FIXERS, "UPPERCASE_MODIFIERS", rogue)
    with pytest.raises(ManualCodingRequiredError):
        correct.apply_fix(facts(line(1, "99213", "rt")), "UPPERCASE_MODIFIERS", 1)


# ---------------------------------------------------------------------------
# Fix, re-run from Layer 1, give up after three
# ---------------------------------------------------------------------------


def test_a_fixable_blocking_finding_is_fixed_and_the_claim_passes() -> None:
    result = correct.run_pipeline(
        [active(rule_doc("UPPERCASE_MODIFIERS", LOWER_RT))], facts(line(1, "99213", "rt"))
    )
    assert result.evaluation.verdict is Verdict.PASS
    assert modifiers(result.facts, 1) == ["RT"]
    assert [c.fix for c in result.corrections] == ["UPPERCASE_MODIFIERS"]
    assert result.rounds == 2


def test_a_finding_with_no_fix_blocks_and_nothing_is_changed() -> None:
    no_fix = active(rule_doc(None, LOWER_RT))
    fixable = active({**rule_doc("STRIP_DIAGNOSIS_DECIMAL", DOTTED), "rule_id": "DOTS"})
    start = facts(line(1, "99213", "rt"), diagnoses=["J06.9"])
    result = correct.run_pipeline([no_fix, fixable], start)
    assert result.evaluation.verdict is Verdict.BLOCK
    assert result.corrections == ()
    assert result.facts == start


@pytest.mark.parametrize("status", [RuleStatus.SHADOW, RuleStatus.ADVISORY])
def test_only_a_blocking_finding_is_ever_fixed(status: RuleStatus) -> None:
    result = correct.run_pipeline(
        [active(rule_doc("UPPERCASE_MODIFIERS", LOWER_RT), status)],
        facts(line(1, "99213", "rt")),
    )
    assert result.corrections == ()
    assert modifiers(result.facts, 1) == ["rt"]


def test_a_fix_that_does_not_clear_its_finding_stops_and_blocks() -> None:
    """The rule flags upper-case RT and its fix upper-cases: nothing changes,
    so the loop ends at once instead of spinning."""
    upper = {"field": "line.modifiers", "op": "contains", "value": "RT"}
    result = correct.run_pipeline(
        [active(rule_doc("UPPERCASE_MODIFIERS", upper))], facts(line(1, "99213", "RT"))
    )
    assert result.evaluation.verdict is Verdict.BLOCK
    assert result.corrections == ()
    assert result.rounds == 1


def test_the_loop_gives_up_after_three_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fix that keeps changing something without clearing its finding is
    stopped at the bound, and the claim goes to a person."""
    counter = iter(range(100))

    def restless(values: list[str]) -> list[str]:
        return [*values[:1], f"A{next(counter)}"]

    monkeypatch.setitem(correct.LINE_FIXERS, "UPPERCASE_MODIFIERS", restless)
    always = {"field": "line.procedure_code", "op": "eq", "value": "99213"}
    result = correct.run_pipeline(
        [active(rule_doc("UPPERCASE_MODIFIERS", always))], facts(line(1, "99213", "RT"))
    )
    assert result.evaluation.verdict is Verdict.BLOCK
    assert result.rounds == correct.MAX_ROUNDS
    # Fixes happen between evaluations: one fewer than the rounds.
    assert len(result.corrections) == correct.MAX_ROUNDS - 1


def test_correcting_leaves_the_input_facts_untouched() -> None:
    start = facts(line(1, "99213", "rt"))
    before = [dict(item.values) for item in start.lines]
    correct.run_pipeline([active(rule_doc("UPPERCASE_MODIFIERS", LOWER_RT))], start)
    assert [dict(item.values) for item in start.lines] == before


def test_a_line_that_is_not_on_the_claim_is_refused() -> None:
    with pytest.raises(ValidationFailedError):
        correct.apply_fix(facts(line(1, "99213", "rt")), "UPPERCASE_MODIFIERS", 9)
