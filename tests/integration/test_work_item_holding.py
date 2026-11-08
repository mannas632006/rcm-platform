"""Taking, giving back and resolving a queue item (M5.4, ADR-0021).

The queue keys `a` (assign to me) and `r` (resolve) call these routes. They
go through the real app, token check, lock and database:

- one holder at a time, refused by name (FR-WORK-01);
- a plain resolve records a structured outcome with its reason, and never
  closes a denial, whose dispositions have consequences (FR-WORK-04);
- an item outside the caller's clients, or in a queue their role cannot
  work, reads as not found (PD-20);
- a retried keystroke acts once (PD-14).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from rcm.platform.audit import AuditEvent
from rcm.platform.auth import Role
from rcm.platform.db import session_scope
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_acknowledgement_loading import CLIENT_A, CLIENT_B
from tests.integration.test_api_shell import api, bearer, clean, items, raise_item  # noqa: F401

pytestmark = pytest.mark.integration

ANA = "ops.ana"
BEN = "ops.ben"


def item_id(key: str) -> uuid.UUID:
    with session_scope() as session:
        found = session.scalar(select(WorkItem.work_item_id).where(WorkItem.source_key == key))
    assert found is not None
    return found


@pytest.fixture
def dq() -> uuid.UUID:
    raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, "dq-1")
    return item_id("dq-1")


def post(
    api: TestClient,  # noqa: F811
    path: str,
    *,
    subject: str = ANA,
    role: Role = Role.AR_SPECIALIST,
    client: uuid.UUID = CLIENT_A,
    key: str | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    headers = {
        **bearer(client, role=role, subject=subject),
        "Idempotency-Key": key or f"hold-{uuid.uuid4()}",
    }
    return api.post(f"/api/v1/work-items/{path}", json=body, headers=headers)


def audited(action: str, entity: uuid.UUID) -> int:
    with session_scope() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == action, AuditEvent.entity_id == entity)
        )
    return count or 0


def test_take_assigns_the_item_to_me(api: TestClient, dq: uuid.UUID) -> None:  # noqa: F811
    response = post(api, f"{dq}/take")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assigned_to"] == ANA
    assert body["state"] == "Assigned"
    assert body["lock_expires_at"] is not None
    page = items(api, "data-quality", assigned="mine")
    assert [row["work_item_id"] for row in page["items"]] == [str(dq)]  # type: ignore[attr-defined]


def test_taking_an_item_another_holds_is_refused_by_name(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    """FR-WORK-01: the second operator is told who holds it, not overwritten."""
    assert post(api, f"{dq}/take", subject=ANA).status_code == 200
    refused = post(api, f"{dq}/take", subject=BEN)
    assert refused.status_code == 409, refused.text
    assert refused.json()["context"]["held_by"] == ANA


def test_taking_my_own_item_again_renews_the_lease(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    first = post(api, f"{dq}/take").json()
    again = post(api, f"{dq}/take")
    assert again.status_code == 200, again.text
    assert again.json()["assigned_to"] == ANA
    assert again.json()["lock_expires_at"] >= first["lock_expires_at"]
    assert audited("work_item.lease_renewed", dq) == 1


def test_release_returns_the_item_to_the_queue(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    post(api, f"{dq}/take")
    released = post(api, f"{dq}/release")
    assert released.status_code == 200, released.text
    assert released.json()["assigned_to"] is None
    assert released.json()["state"] == "Queued"


def test_only_the_holder_may_release(api: TestClient, dq: uuid.UUID) -> None:  # noqa: F811
    post(api, f"{dq}/take", subject=ANA)
    refused = post(api, f"{dq}/release", subject=BEN)
    assert refused.status_code == 409, refused.text


def test_resolve_records_the_outcome_and_why(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    """FR-WORK-04: taken if unheld, then closed with a structured outcome."""
    response = post(
        api,
        f"{dq}/resolution",
        body={"outcome": "NO_ACTION_NEEDED", "note": "Payer corrected the address itself."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "Resolved"
    assert response.json()["outcome"] == "NO_ACTION_NEEDED"
    assert response.json()["assigned_to"] is None
    with session_scope() as session:
        item = session.get(WorkItem, dq)
        assert item is not None
        assert item.resolution_note == "Payer corrected the address itself."
    assert audited("work_item.resolved", dq) == 1
    assert items(api, "data-quality")["items"] == []


def test_resolving_an_item_another_holds_is_refused(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    post(api, f"{dq}/take", subject=BEN)
    refused = post(api, f"{dq}/resolution", body={"outcome": "DUPLICATE", "note": "Same as dq-0."})
    assert refused.status_code == 409, refused.text


def test_a_denial_is_never_resolved_from_the_queue(api: TestClient) -> None:  # noqa: F811
    """A denial's outcomes move money or claims; the workbench decides them."""
    raise_item(CLIENT_A, WorkItemKind.DENIAL, "den-1")
    denial = item_id("den-1")
    refused = post(
        api,
        f"{denial}/resolution",
        role=Role.DENIAL_SPECIALIST,
        body={"outcome": "NO_ACTION_NEEDED", "note": "Looks fine to me."},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["context"]["field"] == "work_item"
    with session_scope() as session:
        item = session.get(WorkItem, denial)
        assert item is not None
        assert item.state != "Resolved"
        assert item.assigned_to is None


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "WRITTEN_OFF", "note": "Not via the queue."},
        {"outcome": "NO_ACTION_NEEDED", "note": ""},
        {"outcome": "NO_ACTION_NEEDED", "note": "   "},
        {"outcome": "NO_ACTION_NEEDED"},
    ],
)
def test_resolve_needs_a_plain_outcome_and_a_reason(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
    body: dict[str, Any],
) -> None:
    refused = post(api, f"{dq}/resolution", body=body)
    assert refused.status_code == 422, refused.text
    with session_scope() as session:
        item = session.get(WorkItem, dq)
        assert item is not None
        assert item.state != "Resolved"


@pytest.mark.parametrize("action", ["take", "release", "resolution"])
def test_another_clients_item_reads_as_not_found(
    api: TestClient,  # noqa: F811
    action: str,
) -> None:
    """PD-20: scoped at the query, the same answer as an item that is not there."""
    raise_item(CLIENT_B, WorkItemKind.DATA_QUALITY, "dq-b")
    other = item_id("dq-b")
    body = {"outcome": "DUPLICATE", "note": "x"} if action == "resolution" else None
    response = post(api, f"{other}/{action}", body=body)
    assert response.status_code == 404, response.text
    assert audited("work_item.claimed", other) == 0


@pytest.mark.parametrize(
    ("role", "kind", "key"),
    [
        # Coders read claims but do not work the claim queues.
        (Role.CODER, WorkItemKind.DATA_QUALITY, "dq-c"),
        # AR specialists see the Denial Workbench but do not decide denials.
        (Role.AR_SPECIALIST, WorkItemKind.DENIAL, "den-c"),
    ],
)
def test_a_role_that_cannot_work_the_queue_cannot_take_from_it(
    api: TestClient,  # noqa: F811
    role: Role,
    kind: WorkItemKind,
    key: str,
) -> None:
    raise_item(CLIENT_A, kind, key)
    target = item_id(key)
    response = post(api, f"{target}/take", role=role)
    assert response.status_code == 404, response.text
    assert audited("work_item.claimed", target) == 0


def test_a_retried_take_acts_once(api: TestClient, dq: uuid.UUID) -> None:  # noqa: F811
    """PD-14: a keystroke sent twice is one claim."""
    key = f"take-once-{uuid.uuid4()}"
    first = post(api, f"{dq}/take", key=key)
    again = post(api, f"{dq}/take", key=key)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert audited("work_item.claimed", dq) == 1
    assert audited("work_item.lease_renewed", dq) == 0


def _set(work_item: uuid.UUID, **values: Any) -> None:
    with session_scope() as session:
        session.execute(update(WorkItem).where(WorkItem.work_item_id == work_item).values(**values))


def test_resolving_my_own_lapsed_hold_takes_it_afresh(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    """Back from lunch before the sweep ran: `r` works as `a` would."""
    post(api, f"{dq}/take")
    _set(dq, lock_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    response = post(
        api, f"{dq}/resolution", body={"outcome": "NO_ACTION_NEEDED", "note": "Fixed upstream."}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "Resolved"


def test_another_operators_lapsed_hold_can_be_taken(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    """FR-WORK-02: an expired lease returns the item; the take is audited."""
    post(api, f"{dq}/take", subject=BEN)
    _set(dq, lock_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    response = post(api, f"{dq}/take", subject=ANA)
    assert response.status_code == 200, response.text
    assert response.json()["assigned_to"] == ANA
    assert audited("work_item.claimed", dq) == 2


def test_a_resolved_item_cannot_be_taken_or_resolved_again(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    body = {"outcome": "DUPLICATE", "note": "Same as dq-0."}
    assert post(api, f"{dq}/resolution", body=body).status_code == 200
    assert post(api, f"{dq}/take").status_code == 409
    assert post(api, f"{dq}/resolution", body=body).status_code == 409
    assert audited("work_item.resolved", dq) == 1


def test_an_escalated_item_is_left_for_a_supervisor(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    _set(dq, state="Escalated")
    body = {"outcome": "NO_ACTION_NEEDED", "note": "Looks fine."}
    for action, payload in (("take", None), ("resolution", body)):
        refused = post(api, f"{dq}/{action}", body=payload)
        assert refused.status_code == 409, refused.text
    with session_scope() as session:
        item = session.get(WorkItem, dq)
        assert item is not None
        assert item.state == "Escalated"
        assert item.assigned_to is None


@pytest.mark.parametrize(
    ("role", "status"),
    [(Role.OPERATIONS_MANAGER, 200), (Role.AR_SPECIALIST, 404), (Role.RULES_AUTHOR, 404)],
)
def test_only_the_operations_manager_works_the_operations_queue(
    api: TestClient,  # noqa: F811
    role: Role,
    status: int,
) -> None:
    """Operations items include compliance concerns, which stop automation
    (BR-EXC-03): ADR-0021."""
    raise_item(CLIENT_A, WorkItemKind.EXTERNAL_SYSTEM_FAILURE, f"ops-{role}")
    target = item_id(f"ops-{role}")
    assert post(api, f"{target}/take", role=role).status_code == status


@pytest.mark.parametrize(
    ("role", "status"), [(Role.RULES_AUTHOR, 200), (Role.OPERATIONS_MANAGER, 404)]
)
def test_only_a_rules_author_works_rules_review(
    api: TestClient,  # noqa: F811
    role: Role,
    status: int,
) -> None:
    raise_item(CLIENT_A, WorkItemKind.RULE_CONFLICT, f"rc-{role}")
    target = item_id(f"rc-{role}")
    assert post(api, f"{target}/take", role=role).status_code == status


@pytest.mark.parametrize("action", ["take", "release", "resolution"])
def test_a_role_without_claim_read_is_refused(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
    action: str,
) -> None:
    body = {"outcome": "DUPLICATE", "note": "x"} if action == "resolution" else None
    assert post(api, f"{dq}/{action}", role=Role.ADMINISTRATOR, body=body).status_code == 403


def test_retried_release_and_resolve_act_once(
    api: TestClient,  # noqa: F811
    dq: uuid.UUID,
) -> None:
    """PD-14 on every route, not only take."""
    post(api, f"{dq}/take")
    key = f"release-{uuid.uuid4()}"
    assert post(api, f"{dq}/release", key=key).json() == post(api, f"{dq}/release", key=key).json()
    assert audited("work_item.released", dq) == 1

    key = f"resolve-{uuid.uuid4()}"
    body = {"outcome": "DUPLICATE", "note": "Same as dq-0."}
    first = post(api, f"{dq}/resolution", key=key, body=body)
    again = post(api, f"{dq}/resolution", key=key, body=body)
    assert first.status_code == again.status_code == 200
    assert audited("work_item.resolved", dq) == 1


def test_a_note_over_the_limit_is_refused(api: TestClient, dq: uuid.UUID) -> None:  # noqa: F811
    refused = post(api, f"{dq}/resolution", body={"outcome": "DUPLICATE", "note": "x" * 2001})
    assert refused.status_code == 422, refused.text
