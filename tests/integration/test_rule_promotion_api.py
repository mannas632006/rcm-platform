"""S-06 Promote over the API (M5.9, UR-30, FR-RULES-06).

The rules list says where Promote would take each version and why it cannot
yet, by the same checks the step makes; the promote route takes exactly that
step or refuses it by name. BIG_CHARGE's backtest corpus gives it a 50%
false-positive rate (tests/integration/test_rule_backtest.py), above the
owner's 10% cap.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, select, text

from rcm.decision.rules.definition import RuleStatus
from rcm.decision.rules.models import RuleStatusChange
from rcm.platform.auth import Role
from rcm.platform.config import get_settings
from rcm.platform.db import reset_engines, session_scope
from tests.integration.test_acknowledgement_loading import CLIENT_A
from tests.integration.test_api_shell import api, bearer  # noqa: F401 - fixture
from tests.integration.test_rule_backtest import ALL, corpus, draft, run
from tests.integration.test_rule_lifecycle import move, status_of

pytestmark = pytest.mark.integration

S = RuleStatus
AUTHOR = Role.RULES_AUTHOR


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


def listed(client: TestClient) -> dict[str, object]:
    body = client.get("/api/v1/rules", headers=bearer(CLIENT_A, role=AUTHOR)).json()
    (rule,) = [r for r in body["rules"] if r["rule_id"] == "BIG_CHARGE"]
    return {**rule, "cap": body["fp_rate_cap"]}


def promote(
    client: TestClient,
    to: RuleStatus,
    *,
    role: Role = AUTHOR,
    reason: str = "backtest reviewed",
) -> Response:
    return client.post(
        "/api/v1/rules/BIG_CHARGE/activate",
        json={"version": 1, "to_status": str(to), "reason": reason},
        headers={
            **bearer(CLIENT_A, role=role, subject="rules.rita"),
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )


def walk_to_advisory(root: Path) -> None:
    corpus(root)
    draft()
    move(S.BACKTESTING)
    run(1)
    move(S.SHADOW)
    move(S.ADVISORY)


def test_promotion_needs_rule_promote(api: TestClient, root: Path) -> None:  # noqa: F811
    """M4.5: reading the rule base is not switching a rule on."""
    corpus(root)
    draft()
    assert promote(api, S.BACKTESTING, role=Role.OPERATIONS_MANAGER).status_code == 403
    assert status_of() is S.DRAFT


@pytest.mark.invariant
def test_the_rules_list_says_why_promotion_is_blocked(api: TestClient, root: Path) -> None:  # noqa: F811
    """UR-30: the disabled Promote states why -- here, no backtest yet --
    and the route refuses the same step."""
    corpus(root)
    draft()
    move(S.BACKTESTING)
    rule = listed(api)
    assert rule["next_status"] == "SHADOW"
    assert "no backtest" in str(rule["promotion_blocked_by"])
    assert rule["cap"] == "0.10"

    assert promote(api, S.SHADOW).status_code == 409
    assert status_of() is S.BACKTESTING


def test_promote_takes_one_step_and_records_who_and_why(api: TestClient, root: Path) -> None:  # noqa: F811
    corpus(root)
    draft()
    move(S.BACKTESTING)
    run(1)
    rule = listed(api)
    assert (rule["next_status"], rule["promotion_blocked_by"]) == ("SHADOW", None)

    response = promote(api, S.SHADOW, reason="50% FP on client A; shadow to watch it")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "rule_id": "BIG_CHARGE",
        "version": 1,
        "from_status": "BACKTESTING",
        "to_status": "SHADOW",
        "fp_rate": "0.5000",
    }
    assert status_of() is S.SHADOW
    with session_scope() as session:
        change = session.scalars(
            select(RuleStatusChange).where(RuleStatusChange.to_status == "SHADOW")
        ).one()
        assert (change.changed_by, change.reason) == (
            "rules.rita",
            "50% FP on client A; shadow to watch it",
        )


def test_a_stale_screen_cannot_promote_twice(api: TestClient, root: Path) -> None:  # noqa: F811
    """The step is named, so a second press from an old screen is refused,
    not taken as the step after it."""
    corpus(root)
    draft()
    move(S.BACKTESTING)
    run(1)
    assert promote(api, S.SHADOW).status_code == 200
    assert promote(api, S.SHADOW).status_code == 409
    assert status_of() is S.SHADOW


@pytest.mark.invariant
def test_a_rule_above_the_cap_shows_why_and_cannot_be_made_blocking(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """FR-RULES-03 through the screen's route: 50% is above the 10% cap. The
    list says so before the press, and the press is refused."""
    walk_to_advisory(root)
    rule = listed(api)
    assert rule["next_status"] == "BLOCKING"
    assert "above the 0.10 cap" in str(rule["promotion_blocked_by"])

    assert promote(api, S.BLOCKING).status_code == 409
    assert status_of() is S.ADVISORY


@pytest.mark.usefixtures("cap")
def test_a_blocking_rule_has_no_promotion_left(api: TestClient, root: Path) -> None:  # noqa: F811
    walk_to_advisory(root)
    assert listed(api)["promotion_blocked_by"] is None
    assert promote(api, S.BLOCKING).status_code == 200
    rule = listed(api)
    assert (rule["next_status"], rule["promotion_blocked_by"]) == (None, None)
