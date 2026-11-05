"""The INSIGHT and ADMIN read routes: dashboard, rules, payer registry.

Through the real app and the real token check. PD-20 for the dashboard (a
client's numbers stay that client's), RBAC for every route.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcm.decision.registry import record_entry
from rcm.domain.payer import Payer
from rcm.platform.auth import Role
from rcm.platform.config import get_settings
from rcm.platform.db import session_scope
from tests.integration.test_acknowledgement_loading import CLIENT_A, CLIENT_B, submit
from tests.integration.test_api_shell import api, bearer, clean  # noqa: F401 - fixtures

pytestmark = pytest.mark.integration

ROUTES = ("/api/v1/dashboard", "/api/v1/rules", "/api/v1/payer-registry")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def test_every_insight_route_needs_a_signed_in_operator(api: TestClient) -> None:  # noqa: F811
    for path in ROUTES:
        assert api.get(path).status_code == 401, path


@pytest.mark.invariant
def test_the_dashboard_counts_only_my_clients_claims(api: TestClient, root: Path) -> None:  # noqa: F811
    """PD-20: client B's claims never appear in client A's numbers."""
    submit("CLM-A1", root, client=CLIENT_A, control="0001")
    submit("CLM-A2", root, client=CLIENT_A, control="0002")
    submit("CLM-B1", root, client=CLIENT_B, control="0003")

    response = api.get("/api/v1/dashboard", headers=bearer(CLIENT_A))
    assert response.status_code == 200
    states = {row["state"]: row["count"] for row in response.json()["claims_by_state"]}
    assert sum(states.values()) == 2

    both = api.get("/api/v1/dashboard", headers=bearer(CLIENT_A, CLIENT_B)).json()
    assert sum(row["count"] for row in both["claims_by_state"]) == 3


def test_an_empty_dashboard_says_no_rate_rather_than_zero(api: TestClient) -> None:  # noqa: F811
    """No adjudicated claims is "unknown", not a 0% denial rate (PD-18)."""
    body = api.get("/api/v1/dashboard", headers=bearer(CLIENT_A)).json()
    assert body["denial_rate"] is None
    assert body["first_pass_clean_rate"] is None


def test_the_dashboard_needs_report_read(api: TestClient) -> None:  # noqa: F811
    response = api.get("/api/v1/dashboard", headers=bearer(CLIENT_A, role=Role.SYSTEM))
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/api/v1/rules", "/api/v1/payer-registry"])
def test_rules_and_registry_need_rule_read(api: TestClient, path: str) -> None:  # noqa: F811
    """A billing specialist works claims; they do not read the rule base."""
    assert api.get(path, headers=bearer(CLIENT_A)).status_code == 403
    allowed = api.get(path, headers=bearer(CLIENT_A, role=Role.RULES_AUTHOR))
    assert allowed.status_code == 200


def test_the_registry_flags_a_stale_entry(api: TestClient, root: Path) -> None:  # noqa: F811
    """BR-REG-02: past [TBD-42] an entry cannot block, and the screen says so."""
    submit("CLM-REG", root, client=CLIENT_A, control="0001")  # creates payer 00590
    today = datetime.now(UTC).date()
    days = get_settings().payer_registry_staleness_days
    with session_scope() as session:
        assert session.get(Payer, "00590") is not None
        for cpt, verified_on in (("99203", today - timedelta(days=days + 1)), ("99213", today)):
            record_entry(
                session,
                payer_id="00590",
                cpt=cpt,
                prior_auth_required=False,
                source_url=None,
                verified_on=verified_on,
                verified_by="ops.ana",
                recorded_by="ops.ana",
                reason="Initial entry from the payer portal",
                today=today,
            )

    body = api.get("/api/v1/payer-registry", headers=bearer(CLIENT_A, role=Role.RULES_AUTHOR))
    entries = {e["cpt"]: e for e in body.json()["entries"]}
    assert entries["99203"]["stale"] is True
    assert entries["99213"]["stale"] is False
    assert entries["99213"]["payer_name"]
    assert body.json()["staleness_days"] == days


def test_rules_list_answers_with_its_shape(api: TestClient) -> None:  # noqa: F811
    response = api.get("/api/v1/rules", headers=bearer(CLIENT_A, role=Role.OPERATIONS_MANAGER))
    assert response.status_code == 200
    assert isinstance(response.json()["rules"], list)
