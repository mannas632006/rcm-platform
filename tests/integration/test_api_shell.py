"""The application shell's API (M5.1, SRS 10.4.0, UR-26, PD-20).

Every screen reads these: who is signed in, the queues they may see, and the
filing-deadline alert. They go through the real app, the real token check
and the real database.
"""

from __future__ import annotations

import base64
import json
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
from rcm.platform.audit import AuditEvent
from rcm.platform.auth import Role, issue_dev_token
from rcm.platform.config import get_settings
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ValidationFailedError
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


# ---------------------------------------------------------------------------
# A queue's items (M5.3, S-02)
# ---------------------------------------------------------------------------


def scored(owner_engine: Engine, key: str, score: int) -> None:
    """Give a raised item a priority score, as prioritisation would."""
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE work_item SET priority_score = :s, prioritised_at = now() "
                "WHERE source_key = :k"
            ),
            {"s": score, "k": key},
        )


def items(api: TestClient, queue: str, **params: str) -> dict[str, object]:
    response = api.get(
        f"/api/v1/work-queues/{queue}/items", params=params, headers=bearer(CLIENT_A)
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def titles(page: dict[str, object]) -> list[str]:
    rows = page["items"]
    assert isinstance(rows, list)
    # Titles read "<problem>, <key> (claim abcd1234)": keep the key.
    return [str(row["title"]).rsplit(", ", 1)[-1].split(" (", 1)[0] for row in rows]


@pytest.mark.invariant
def test_a_queue_lists_its_items_best_first(api: TestClient, owner_engine: Engine) -> None:
    """UR-02: priority, not age. The order is priority.queue_order's."""
    for key, score in (("low", 10), ("high", 90), ("mid", 50)):
        raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, key)
        scored(owner_engine, key, score)
    assert titles(items(api, "data-quality")) == ["high", "mid", "low"]


@pytest.mark.invariant
def test_pages_never_repeat_or_skip_an_item(api: TestClient, owner_engine: Engine) -> None:
    """FR-API-02: keyset pages, so the whole queue is read exactly once."""
    keys = [f"item-{n}" for n in range(7)]
    for n, key in enumerate(keys):
        raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, key)
        scored(owner_engine, key, 10 + n)
    seen: list[str] = []
    page = items(api, "data-quality", limit="3")
    while True:
        seen.extend(titles(page))
        cursor = page["next_cursor"]
        if cursor is None:
            break
        page = items(api, "data-quality", limit="3", cursor=str(cursor))
    assert seen == list(reversed(keys))


@pytest.mark.invariant
def test_a_filter_never_hides_deadline_pinned_work(api: TestClient) -> None:
    """UR-03: pinned items stay in view whatever the filter."""
    raise_item(CLIENT_A, WorkItemKind.DEADLINE_RISK, "pinned")
    raise_item(CLIENT_A, WorkItemKind.SUBMISSION_REJECTION, "ordinary")
    page = items(api, "ar-follow-up", assigned="mine")
    assert titles(page) == ["pinned"]
    rows = page["items"]
    assert isinstance(rows, list)
    assert rows[0]["deadline_pinned"] is True


def test_a_queue_shows_only_my_clients_items(api: TestClient) -> None:
    """PD-20."""
    raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, "mine")
    raise_item(CLIENT_B, WorkItemKind.DATA_QUALITY, "theirs")
    assert titles(items(api, "data-quality")) == ["mine"]


def test_a_queue_or_cursor_that_is_not_mine_is_refused(api: TestClient) -> None:
    """A queue the role may not see looks like no queue at all; a cursor we
    did not issue is refused, never read as "from the start" (PD-18)."""
    coder = bearer(CLIENT_A, role=Role.CODER)
    assert api.get("/api/v1/work-queues/denials/items", headers=coder).status_code == 404
    assert api.get("/api/v1/work-queues/nonsense/items", headers=coder).status_code == 404
    bad = api.get(
        "/api/v1/work-queues/data-quality/items",
        params={"cursor": "not-a-cursor"},
        headers=bearer(CLIENT_A),
    )
    assert bad.status_code == 422


@pytest.mark.parametrize(
    "keys",
    [
        # The id as a number, not a string.
        ["1", "0", "1", "-50", "2026-11-10T00:00:00+00:00", "2026-11-01T00:00:00+00:00", 5],
        # Infinity: json.loads accepts it; int() of it overflows.
        [
            "1",
            "0",
            "1",
            "Infinity",
            "2026-11-10T00:00:00+00:00",
            "2026-11-01T00:00:00+00:00",
            str(uuid.uuid4()),
        ],
        # No offset: would be read in the session's zone.
        [
            "1",
            "0",
            "1",
            "-50",
            "2026-11-10T00:00:00",
            "2026-11-01T00:00:00+00:00",
            str(uuid.uuid4()),
        ],
        {"not": "a list"},
    ],
)
def test_a_tampered_cursor_is_refused_by_name(keys: object) -> None:
    """PD-18: a forged cursor gets the named 422, never a bare 500."""
    raw = json.dumps(keys).replace('"Infinity"', "Infinity")
    cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    with pytest.raises(ValidationFailedError):
        views._decode(cursor)


@pytest.mark.invariant
def test_reading_a_queue_is_audited(api: TestClient) -> None:
    """PD-2: rows carry claim identifiers and dates of service."""
    subject = f"auditor.{uuid.uuid4().hex[:8]}"
    response = api.get(
        "/api/v1/work-queues/data-quality/items", headers=bearer(CLIENT_A, subject=subject)
    )
    assert response.status_code == 200
    with session_scope() as session:
        actions = session.scalars(
            select(AuditEvent.action).where(AuditEvent.actor_id == subject)
        ).all()
    assert actions == ["GET /api/v1/work-queues/data-quality/items"]


@pytest.mark.invariant
def test_a_read_that_cannot_be_audited_is_refused(api: TestClient) -> None:
    """PD-2 / PD-18: the audit row is written before anything is read. If it
    cannot be written -- here a subject too long for the audit column -- the
    request fails; it never returns PHI with no record of the access."""
    response = api.get(
        "/api/v1/work-queues/data-quality/items", headers=bearer(CLIENT_A, subject="x" * 200)
    )
    assert response.status_code == 500
    assert "items" not in response.json()
