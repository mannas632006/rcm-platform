"""Open specification values: reported while open, held once decided
(CLAUDE.md section 12, ADR-0031).

A value nobody has decided is reported at startup. A value the owner has
decided is not, and its default cannot drift from the decision: changing it
takes another decision, recorded in `placeholders.DECIDED`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rcm.platform.config import Settings
from rcm.platform.placeholders import DECIDED, DECLARED, REGISTRY


def test_a_decided_value_is_no_longer_reported_as_open() -> None:
    unresolved = Settings().unresolved_placeholders()
    assert set(unresolved) == set(REGISTRY)
    assert not set(unresolved) & set(DECIDED)


def test_what_is_still_open_is_still_reported() -> None:
    """The owner decided what was put to them on 2026-11-12, not everything."""
    assert set(REGISTRY) == {
        "next_action_days",
        "exception_sla_hours",
        "exception_trend_ratio",
        "exception_trend_min_count",
        # Raised after the decisions, by ADR-0033.
        "status_remittance_grace_days",
        "status_pending_repeat_limit",
    }


@pytest.mark.parametrize("name", sorted(DECIDED))
def test_a_decided_setting_defaults_to_the_decision(name: str) -> None:
    decision = DECIDED[name]
    default = getattr(Settings(), name)
    expected = Decimal(decision.value) if isinstance(default, Decimal) else decision.value
    assert default == expected
    assert decision.decided_by.strip()


def test_every_decision_is_about_a_declared_value() -> None:
    assert set(DECIDED) <= set(DECLARED)
    # The decision adopted the provisional value as it stood (ADR-0031).
    assert {n: d.value for n, d in DECIDED.items()} == {n: DECLARED[n].provisional for n in DECIDED}


def test_an_explicit_setting_resolves_an_open_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCM_EXCEPTION_TREND_MIN_COUNT", "25")
    assert "exception_trend_min_count" not in Settings().unresolved_placeholders()
