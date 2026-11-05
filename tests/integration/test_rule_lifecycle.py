"""Rule lifecycle and the false-positive cap (M4.5, M4.6, ADR-0010).

FR-RULES-06: draft -> backtest -> shadow -> advisory -> blocking. No shortcuts.
FR-RULES-03: a rule above [TBD-45] (owner: 10%) may not be BLOCKING; it may be
ADVISORY.

The backtest corpus (tests/integration/test_rule_backtest.py) gives
BIG_CHARGE a false-positive rate of 50%.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ProgrammingError

from rcm.decision.rules import lifecycle
from rcm.decision.rules.definition import RuleStatus
from rcm.decision.rules.models import RuleStatusChange, RuleVersion
from rcm.platform.config import get_settings
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ResourceNotFoundError, StateTransitionError, ValidationFailedError
from tests.integration.test_acknowledgement_loading import (
    CLIENT_A,
    CLIENT_B,
    denial_for,
    payment_for,
    remit,
)
from tests.integration.test_rule_backtest import ALL, BIG_CHARGES, corpus, draft, load, run

pytestmark = pytest.mark.integration

S = RuleStatus


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE rule_status_change, {ALL} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


@pytest.fixture
def cap(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Raise the cap above BIG_CHARGE's 50%, to show the happy path."""
    monkeypatch.setenv("RCM_BLOCKING_RULE_MAX_FALSE_POSITIVE_RATE", "0.60")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("RCM_BLOCKING_RULE_MAX_FALSE_POSITIVE_RATE")
    get_settings.cache_clear()


def move(to: RuleStatus, version: int = 1, reason: str = "reviewed") -> lifecycle.StatusChange:
    with session_scope() as session:
        return lifecycle.change_status(
            session,
            rule_id="BIG_CHARGE",
            version=version,
            to=to,
            changed_by="ar_lead",
            reason=reason,
        )


def status_of(version: int = 1) -> RuleStatus:
    with session_scope() as session:
        row = session.get(RuleVersion, ("BIG_CHARGE", version))
        assert row is not None
        return RuleStatus(row.status)


def walk_to_shadow(root: Path) -> None:
    corpus(root)
    draft()
    move(S.BACKTESTING)
    run(1)
    move(S.SHADOW)


def test_the_lifecycle_is_declared_as_data() -> None:
    """SRS 8.3, as a table: every allowed step, and only those."""
    assert {
        S.DRAFT: frozenset({S.BACKTESTING}),
        S.BACKTESTING: frozenset({S.DRAFT, S.SHADOW}),
        S.SHADOW: frozenset({S.ADVISORY}),
        S.ADVISORY: frozenset({S.BLOCKING, S.DRAFT}),
        S.BLOCKING: frozenset({S.DEPRECATED, S.DRAFT}),
        S.DEPRECATED: frozenset(),
    } == lifecycle.TRANSITIONS


@pytest.mark.invariant
def test_rule_promotion_requires_backtest(root: Path) -> None:
    """CLAUDE.md 7.2 / M4.5: promotion without a backtest is impossible."""
    corpus(root)
    draft()
    move(S.BACKTESTING)
    with pytest.raises(StateTransitionError) as refused:
        move(S.SHADOW)
    assert "backtest" in refused.value.context["reason"]
    assert status_of() is S.BACKTESTING


def test_a_backtest_of_another_version_does_not_count(root: Path) -> None:
    corpus(root)
    draft()
    run(1)
    draft({**BIG_CHARGES, "severity": "ADVISORY"})
    move(S.BACKTESTING, version=2)
    with pytest.raises(StateTransitionError):
        move(S.SHADOW, version=2)


@pytest.mark.invariant
def test_no_step_can_be_skipped(root: Path) -> None:
    """FR-RULES-06: "No shortcuts"."""
    corpus(root)
    draft()
    run(1)
    for target in (S.SHADOW, S.ADVISORY, S.BLOCKING):
        with pytest.raises(StateTransitionError):
            move(target)
    assert status_of() is S.DRAFT


@pytest.mark.invariant
def test_a_rule_above_the_fp_cap_cannot_block(root: Path) -> None:
    """FR-RULES-03 / M4.6 at the owner's 10%: BIG_CHARGE measured 50%."""
    walk_to_shadow(root)
    move(S.ADVISORY)
    with pytest.raises(StateTransitionError) as refused:
        move(S.BLOCKING)
    assert "false-positive" in refused.value.context["reason"]
    assert status_of() is S.ADVISORY


def test_the_default_cap_is_the_owners_ten_percent() -> None:
    assert get_settings().blocking_rule_max_false_positive_rate == Decimal("0.10")


@pytest.mark.usefixtures("cap")
def test_a_rule_under_the_cap_reaches_blocking(root: Path) -> None:
    walk_to_shadow(root)
    move(S.ADVISORY)
    change = move(S.BLOCKING)
    assert status_of() is S.BLOCKING
    assert change.fp_rate == Decimal("0.5000")


@pytest.mark.usefixtures("cap")
def test_the_worst_client_decides(root: Path) -> None:
    """A rule backtested on two clients blocks only if both pass: the cap is
    measured on the client where the rule does worst."""
    walk_to_shadow(root)
    load("CLM-B1", "100.00", root, control="0098", client=CLIENT_B)
    load("CLM-B2", "900.00", root, control="0099", client=CLIENT_B)
    remit(denial_for("CLM-B1", control="0098"), root, client=CLIENT_B, name="b1.835")
    remit(payment_for("CLM-B2", control="0099"), root, client=CLIENT_B, name="b2.835")
    assert run(1, CLIENT_B).tally.fp_rate == Decimal("1.0000")
    move(S.ADVISORY)
    with pytest.raises(StateTransitionError):
        move(S.BLOCKING)


@pytest.mark.usefixtures("cap")
def test_a_rule_that_flagged_nothing_cannot_block(root: Path) -> None:
    """A null FP rate is no evidence of accuracy (ADR-0009)."""
    corpus(root)
    draft({**BIG_CHARGES, "condition": {"field": "claim.line_count", "op": "gt", "value": 50}})
    move(S.BACKTESTING)
    assert run(1).tally.fp_rate is None
    move(S.SHADOW)
    move(S.ADVISORY)
    with pytest.raises(StateTransitionError):
        move(S.BLOCKING)


@pytest.mark.usefixtures("cap")
def test_an_advisory_rule_cannot_be_made_blocking(root: Path) -> None:
    """Severity is the author's ceiling: an ADVISORY rule never blocks."""
    corpus(root)
    draft({**BIG_CHARGES, "severity": "ADVISORY"})
    move(S.BACKTESTING)
    run(1)
    move(S.SHADOW)
    move(S.ADVISORY)
    with pytest.raises(StateTransitionError):
        move(S.BLOCKING)


def test_a_second_live_version_is_refused_by_name(root: Path) -> None:
    walk_to_shadow(root)
    draft({**BIG_CHARGES, "severity": "ADVISORY"})
    move(S.BACKTESTING, version=2)
    run(2)
    with pytest.raises(StateTransitionError) as refused:
        move(S.SHADOW, version=2)
    assert "live" in refused.value.context["reason"]


@pytest.mark.invariant
def test_every_status_change_is_recorded(root: Path) -> None:
    walk_to_shadow(root)
    with session_scope() as session:
        rows = session.scalars(select(RuleStatusChange).order_by(RuleStatusChange.sequence)).all()
    assert [(r.from_status, r.to_status) for r in rows] == [
        ("DRAFT", "BACKTESTING"),
        ("BACKTESTING", "SHADOW"),
    ]
    assert rows[0].backtest_id is None
    assert rows[1].backtest_id is not None
    assert all(r.changed_by == "ar_lead" and r.reason for r in rows)


def test_a_change_needs_a_reason(root: Path) -> None:
    corpus(root)
    draft()
    with pytest.raises(ValidationFailedError):
        move(S.BACKTESTING, reason=" ")


def test_an_unknown_rule_cannot_change() -> None:
    with pytest.raises(ResourceNotFoundError):
        move(S.BACKTESTING, version=9)


def test_a_status_change_record_cannot_be_rewritten(root: Path, app_engine: Engine) -> None:
    corpus(root)
    draft()
    move(S.BACKTESTING)
    for statement in (
        "UPDATE rule_status_change SET reason = 'x'",
        "DELETE FROM rule_status_change",
    ):
        with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
            conn.execute(text(statement))
        assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)


def test_one_clients_backtest_is_enough_to_shadow(root: Path) -> None:
    """Shadow decides nothing about any claim, so one client's evidence is
    enough to start watching. Blocking takes every client's (above)."""
    corpus(root)
    draft()
    move(S.BACKTESTING)
    run(1, CLIENT_A)
    move(S.SHADOW)
    assert status_of() is S.SHADOW
