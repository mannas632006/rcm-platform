"""Queue prioritisation rules (M3.6, SRS 7.8, DOC-02 BR-GLOBAL-04).

PD-15: Timely-filing deadline overrides all other prioritisation.
FR-WORK-05 (MUST): Deadline-pinned items bypass the scoring model
entirely. No model output may demote a claim about to expire.
FR-WORK-06 (SHOULD): The queue exposes *why* an item ranks where it does.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcm.decision import prioritisation
from rcm.decision.prioritisation import LOGIC_VERSION, PriorityInputs, prioritise

ESCALATION_DAYS = 14
BULK = Decimal("25.00")


def inputs(**overrides: object) -> PriorityInputs:
    values: dict[str, object] = {
        "kind": "DENIAL",
        "balance": Decimal("500.00"),
        "days_to_deadline": 200,
        "age_days": 5,
        "has_claim": True,
    }
    values.update(overrides)
    return PriorityInputs(**values)  # type: ignore[arg-type]


def rank(i: PriorityInputs) -> tuple[bool, int]:
    r = prioritise(i, escalation_days=ESCALATION_DAYS, bulk_threshold=BULK)
    return (r.pinned, r.score)


@pytest.mark.invariant
def test_deadline_beats_priority_score() -> None:
    """PD-15: a low-value claim three days from its filing deadline outranks
    a high-value one with most of a year to go."""
    expiring_cheap = inputs(balance=Decimal("20.00"), days_to_deadline=3)
    fresh_valuable = inputs(balance=Decimal("50000.00"), days_to_deadline=300)

    assert rank(expiring_cheap) > rank(fresh_valuable)
    assert rank(expiring_cheap) == (True, 100)
    assert rank(fresh_valuable)[1] <= 99


def test_a_pinned_item_never_consults_the_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-WORK-05: *bypass the scoring model entirely* -- not "score it and
    then override", which leaves a path for a score to matter."""

    def explode(*_: object, **__: object) -> int:
        raise AssertionError("the scorer was consulted for a pinned item")

    monkeypatch.setattr(prioritisation, "_score", explode)
    result = prioritise(
        inputs(days_to_deadline=2), escalation_days=ESCALATION_DAYS, bulk_threshold=BULK
    )
    assert result.pinned and result.score == 100


def test_the_threshold_day_itself_is_pinned() -> None:
    assert rank(inputs(days_to_deadline=ESCALATION_DAYS)) == (True, 100)
    assert rank(inputs(days_to_deadline=ESCALATION_DAYS + 1))[0] is False


def test_a_missed_deadline_is_still_pinned() -> None:
    """Past the deadline is not "no longer urgent": it needs a person now,
    to appeal on proof of timely filing or to write it off on purpose."""
    assert rank(inputs(days_to_deadline=-10)) == (True, 100)


def test_an_unknown_deadline_is_pinned() -> None:
    """Owner decision (2026-11-01), PD-18: an unknown deadline is treated as
    urgent, never as far away."""
    result = prioritise(
        inputs(days_to_deadline=None), escalation_days=ESCALATION_DAYS, bulk_threshold=BULK
    )
    assert (result.pinned, result.score) == (True, 100)
    assert "unknown" in result.reason


def test_an_item_with_no_claim_has_no_deadline_to_pin_on() -> None:
    """A payer-match review is not about a claim; it has no filing deadline
    and is scored, not pinned for want of one."""
    result = prioritise(
        inputs(has_claim=False, days_to_deadline=None, balance=Decimal("0")),
        escalation_days=ESCALATION_DAYS,
        bulk_threshold=BULK,
    )
    assert result.pinned is False


def test_more_money_ranks_higher() -> None:
    assert rank(inputs(balance=Decimal("5000"))) > rank(inputs(balance=Decimal("50")))


def test_less_effort_ranks_higher_for_the_same_money() -> None:
    """SRS 7.8: divide by estimated effort. Two kinds with the same recovery
    odds and balance; the one that takes half the time ranks higher."""
    quick, slow = "DATA_QUALITY", "DENIAL"
    assert prioritisation.P_RECOVERY[quick] == prioritisation.P_RECOVERY[slow]
    assert prioritisation.EFFORT_HOURS[quick] < prioritisation.EFFORT_HOURS[slow]
    assert rank(inputs(kind=quick)) > rank(inputs(kind=slow))


def test_a_nearer_deadline_ranks_higher_before_it_pins() -> None:
    """DOC-02: the deadline multiplier escalates as timely filing approaches."""
    assert rank(inputs(days_to_deadline=20)) > rank(inputs(days_to_deadline=300))


def test_older_items_decay() -> None:
    assert rank(inputs(age_days=1)) > rank(inputs(age_days=400))


def test_small_balances_go_to_bulk_handling() -> None:
    """BR-M10-06: expected recovery below [TBD-46] is bulk work."""
    result = prioritise(
        inputs(balance=Decimal("10.00")), escalation_days=ESCALATION_DAYS, bulk_threshold=BULK
    )
    assert result.bulk is True


def test_a_pinned_item_is_never_bulk() -> None:
    result = prioritise(
        inputs(balance=Decimal("1.00"), days_to_deadline=1),
        escalation_days=ESCALATION_DAYS,
        bulk_threshold=BULK,
    )
    assert result.pinned and not result.bulk


def test_the_result_explains_itself() -> None:
    """FR-WORK-06 and PD-1: the components and the logic version travel
    with the score."""
    result = prioritise(inputs(), escalation_days=ESCALATION_DAYS, bulk_threshold=BULK)
    assert result.logic_version == LOGIC_VERSION
    for part in ("expected_value", "p_recovery", "effort_hours", "age_decay", "urgency"):
        assert part in result.components
    assert result.reason


@given(
    balance=st.decimals(min_value=0, max_value=10_000_000, places=2),
    days=st.one_of(st.none(), st.integers(min_value=-1000, max_value=5000)),
    age=st.integers(min_value=0, max_value=5000),
    kind=st.sampled_from(sorted(prioritisation.P_RECOVERY)),
)
def test_scores_stay_on_the_scale(balance: Decimal, days: int | None, age: int, kind: str) -> None:
    """0-99 for scored items, exactly 100 for pinned ones, and nothing else."""
    result = prioritise(
        inputs(balance=balance, days_to_deadline=days, age_days=age, kind=kind),
        escalation_days=ESCALATION_DAYS,
        bulk_threshold=BULK,
    )
    if result.pinned:
        assert result.score == 100
    else:
        assert 0 <= result.score <= 99
