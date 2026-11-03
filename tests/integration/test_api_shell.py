"""The application shell's API (M5.1, SRS 10.4.0, UR-26, PD-20).

Every screen reads these: who is signed in, the queues they may see, and the
filing-deadline alert. They go through the real app, the real token check
and the real database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from rcm.api.main import create_app
from rcm.domain.claim import Claim
from rcm.platform.auth import Role, issue_dev_token
from rcm.platform.config import get_settings
from rcm.platform.db import reset_engines, session_scope
from rcm.work import exceptions, views
from rcm.work.models import WorkItemKind
from tests.integration.test_acknowledgement_loading import CHARGE, CLIENT_A, CLIENT_B, submit
from tests.integration.test_claim_progression import TABLES

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("RCM_ENVIRONMENT", "ci")
    monkeypatch.setenv("RCM_DEV_JWT_SECRET", "m5-shell-test-secret-at-least-32-bytes")
    get_settings.cache_clear()
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client
    get_settings.cache_clear()


def bearer(
    *clients: uuid.UUID, role: Role = Role.AR_SPECIALIST, subject: str = "ops.ana"
) -> dict[str, str]:
    token = issue_dev_token(subject=subject, roles=[role], client_ids=list(clients))
    return {"Authorization": f"Bearer {token}"}


def raise_item(
    client: uuid.UUID, kind: WorkItemKind, key: str, *, claim_id: uuid.UUID | None = None
) -> None:
    with session_scope() as session:
        exceptions.raise_exception(
            session,
            client_id=client,
            kind=kind,
            problem=f"A named test problem for the shell API, {key}",
            record=exceptions.RecordRef("claim", claim_id or uuid.uuid4()),
            source_key=key,
            now=NOW,
            claim_id=claim_id,
        )


def queue_keys(api: TestClient, role: Role) -> set[str]:
    response = api.get("/api/v1/work-queues", headers=bearer(CLIENT_A, role=role))
    return {q["key"] for q in response.json()["queues"]}


def test_every_route_needs_a_signed_in_operator(api: TestClient) -> None:
    for path in ("/api/v1/me", "/api/v1/work-queues", "/api/v1/alerts/deadline-risk"):
        response = api.get(path)
        assert response.status_code == 401, path
        assert response.headers["content-type"].startswith("application/problem+json")


def test_me_says_who_is_signed_in_and_what_they_may_do(api: TestClient) -> None:
    response = api.get("/api/v1/me", headers=bearer(CLIENT_A, role=Role.DENIAL_SPECIALIST))
    assert response.status_code == 200
    me = response.json()
    assert me["subject"] == "ops.ana"
    assert me["roles"] == ["DENIAL_SPECIALIST"]
    assert "denial:read" in me["permissions"]
    assert me["client_ids"] == [str(CLIENT_A)]


@pytest.mark.invariant
def test_queue_counts_never_include_another_clients_work(api: TestClient) -> None:
    """PD-20, through the API: client B's work is not in client A's counts."""
    raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, "a-1")
    raise_item(CLIENT_B, WorkItemKind.DATA_QUALITY, "b-1")
    raise_item(CLIENT_B, WorkItemKind.DATA_QUALITY, "b-2")
    queues = api.get("/api/v1/work-queues", headers=bearer(CLIENT_A)).json()["queues"]
    (data_quality,) = [q for q in queues if q["key"] == "data-quality"]
    assert (data_quality["open"], data_quality["unassigned"], data_quality["mine"]) == (1, 1, 0)


def test_a_principal_with_no_client_sees_no_work(api: TestClient) -> None:
    """PD-18: an empty scope is nothing, never everything."""
    raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, "a-1")
    queues = api.get("/api/v1/work-queues", headers=bearer()).json()["queues"]
    assert all(q["open"] == 0 for q in queues)


def test_queues_are_listed_only_to_who_may_work_them(api: TestClient) -> None:
    """A coder holds no denial permission, so no Denial Workbench."""
    assert "denials" not in queue_keys(api, Role.CODER)
    assert "denials" in queue_keys(api, Role.DENIAL_SPECIALIST)


def test_every_work_item_kind_is_in_exactly_one_queue() -> None:
    """A kind in no queue would be work no screen shows."""
    for kind in WorkItemKind:
        assert sum(kind in q.kinds for q in views.QUEUES) == 1, kind


@pytest.mark.invariant
def test_the_deadline_alert_counts_pinned_claims_and_their_charges(
    api: TestClient, tmp_path: Path
) -> None:
    """UR-26 / PD-15: only deadline-pinned work, only my clients' claims."""
    root = tmp_path / "raw"
    root.mkdir()
    submit("CLM-LATE", root, control="0001")
    submit("CLM-OTHER", root, client=CLIENT_B, control="0002")
    with session_scope() as session:
        rows = session.execute(select(Claim.submitter_claim_id, Claim.claim_id)).all()
    ids = dict(rows)
    raise_item(CLIENT_A, WorkItemKind.DEADLINE_RISK, "late", claim_id=ids["CLM-LATE"])
    raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, "not-pinned", claim_id=ids["CLM-LATE"])
    raise_item(CLIENT_B, WorkItemKind.DEADLINE_RISK, "other", claim_id=ids["CLM-OTHER"])

    risk = api.get("/api/v1/alerts/deadline-risk", headers=bearer(CLIENT_A)).json()
    assert risk["claims"] == 1
    assert Decimal(risk["charges"]) == CHARGE


def test_money_crosses_the_api_as_an_exact_decimal_string(api: TestClient) -> None:
    """UR-21: a float would have lost cents before the UI formatted it, and
    the scale is always the cent -- "0.00", never "0"."""
    risk = api.get("/api/v1/alerts/deadline-risk", headers=bearer(CLIENT_A)).json()
    assert risk["charges"] == "0.00"


def test_the_deadline_alert_needs_claim_read(api: TestClient) -> None:
    """Billed charges are claim money: a role without claim:read is refused,
    whatever its client scope (S-4)."""
    response = api.get(
        "/api/v1/alerts/deadline-risk", headers=bearer(CLIENT_A, role=Role.ADMINISTRATOR)
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
