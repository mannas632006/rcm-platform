"""Open specification values: reported while open, held once decided
(CLAUDE.md section 12, ADR-0031, ADR-0035).

A value nobody has decided is reported at startup. A value the owner has
decided is not, and its default cannot drift from the decision: changing it
takes another decision, recorded in `placeholders.DECIDED`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from rcm.platform import config
from rcm.platform.config import Settings
from rcm.platform.placeholders import DECIDED, DECLARED, REGISTRY, Placeholder


def test_a_decided_value_is_no_longer_reported_as_open() -> None:
    unresolved = Settings().unresolved_placeholders()
    assert set(unresolved) == set(REGISTRY)
    assert not set(unresolved) & set(DECIDED)


def test_every_declared_value_is_decided() -> None:
    """By 2026-11-13 the owner had decided every value put to them (ADR-0035)."""
    assert REGISTRY == {}
    assert set(DECIDED) == set(DECLARED)


@pytest.mark.parametrize("name", sorted(DECIDED))
def test_a_decided_setting_defaults_to_the_decision(name: str) -> None:
    decision = DECIDED[name]
    default = getattr(Settings(), name)
    expected = Decimal(decision.value) if isinstance(default, Decimal) else decision.value
    assert default == expected
    assert decision.decided_by.strip()


def test_every_decision_is_about_a_declared_value() -> None:
    assert set(DECIDED) <= set(DECLARED)
    # ADR-0031 adopted each provisional value as it stood.
    adopted = {n for n, d in DECIDED.items() if d.decided_on == date(2026, 11, 12)}
    assert {n: DECIDED[n].value for n in adopted} == {n: DECLARED[n].provisional for n in adopted}


def test_the_owner_chose_a_tighter_follow_up_cadence() -> None:
    """ADR-0035: 21 days with the payer, not 30; 5 after a decision, not 7."""
    days = Settings().next_action_days
    assert days["InAdjudication"] == 21
    assert days["Paid"] == days["PartiallyPaid"] == days["Denied"] == 5
    assert (days["*"], days["Submitted"]) == (1, 2)


def test_an_explicit_setting_resolves_an_open_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mechanism outlives the values: an open one is reported until set."""
    open_value = Placeholder(
        tbd_id="TBD-00",
        description="A stand-in for a value raised later.",
        owner="Muhammad Anas",
        provisional=10,
        source="test",
    )
    monkeypatch.setattr(config, "REGISTRY", {"exception_trend_min_count": open_value})
    assert "exception_trend_min_count" in Settings().unresolved_placeholders()
    monkeypatch.setenv("RCM_EXCEPTION_TREND_MIN_COUNT", "25")
    assert "exception_trend_min_count" not in Settings().unresolved_placeholders()
