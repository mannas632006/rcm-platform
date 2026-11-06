"""S-04 Denial Workbench, read side (M5.7 slice 1).

Through the real app, loaders, progression and denial routing. A denial is
a PHI read: scope (PD-20) and audit (PD-2) first. Then what the operator
needs to decide: each denied line with group and reason together (PD-3),
its category and preventability as classified (BR-TAX-01), and how the
same denial went before (UR-11).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rcm.decision.denial_models import DenialClassification
from rcm.platform.audit import AuditEvent
from rcm.platform.auth import Role
from rcm.platform.db import session_scope
from rcm.work import queue
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import WorkItemOutcome
from tests.integration.test_acknowledgement_loading import (
    CLIENT_A,
    CLIENT_B,
    denial_for,
    remit,
    submit,
)
from tests.integration.test_api_shell import api, bearer, clean, raise_item  # noqa: F401
from tests.integration.test_denial_classification import NOW, route

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def denied(root: Path, submitter_id: str, control: str) -> uuid.UUID:
    """A claim denied CO-50, routed to the workbench; its work item id."""
    submit(submitter_id, root, control=control)
    remit(denial_for(submitter_id, control=control), root, name=f"{submitter_id}.835")
    route()
    with session_scope() as session:
        item = session.scalar(
            select(WorkItem.work_item_id)
            .where(WorkItem.kind == str(WorkItemKind.DENIAL))
            .order_by(WorkItem.created_at.desc())
            .limit(1)
        )
    assert item is not None
    return item


def workbench(api: TestClient, item: uuid.UUID, *clients: uuid.UUID, **kw: Any) -> Any:  # noqa: F811
    role = kw.pop("role", Role.DENIAL_SPECIALIST)
    return api.get(
        f"/api/v1/denials/{item}", headers=bearer(*(clients or (CLIENT_A,)), role=role, **kw)
    )


@pytest.mark.invariant
def test_cross_client_access_denied_for_denials(api: TestClient, root: Path) -> None:  # noqa: F811
    """PD-20: same 404 as a work item that was never raised."""
    item = denied(root, "CLM-X1", "0001")
    other = workbench(api, item, CLIENT_B)
    unknown = workbench(api, uuid.uuid4(), CLIENT_B)
    assert other.status_code == unknown.status_code == 404


def test_the_workbench_needs_denial_read(api: TestClient, root: Path) -> None:  # noqa: F811
    item = denied(root, "CLM-X2", "0001")
    assert workbench(api, item, role=Role.ADMINISTRATOR).status_code == 403


@pytest.mark.invariant
def test_reading_a_denial_is_audited(api: TestClient, root: Path) -> None:  # noqa: F811
    item = denied(root, "CLM-X3", "0001")
    subject = f"auditor.{uuid.uuid4().hex[:8]}"
    assert workbench(api, item, subject=subject).status_code == 200
    with session_scope() as session:
        actions = session.scalars(
            select(AuditEvent.action).where(AuditEvent.actor_id == subject)
        ).all()
    assert actions == [f"GET /api/v1/denials/{item}"]


def test_a_work_item_that_is_not_a_denial_is_not_found(api: TestClient) -> None:  # noqa: F811
    """The workbench opens denials only; any other item reads as absent."""
    raise_item(CLIENT_A, WorkItemKind.DATA_QUALITY, "dq-not-a-denial")
    with session_scope() as session:
        other = session.scalar(
            select(WorkItem.work_item_id).where(WorkItem.kind == str(WorkItemKind.DATA_QUALITY))
        )
    assert other is not None
    assert workbench(api, other).status_code == 404


@pytest.mark.invariant
def test_each_denied_line_shows_its_codes_and_classification(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """PD-3 and BR-TAX-01: group and reason together, and the one category
    and preventability the classifier recorded -- not a re-derivation."""
    item = denied(root, "CLM-X4", "0001")
    body = workbench(api, item).json()
    assert body["claim"]["submitter_claim_id"] == "CLM-X4"
    (line,) = body["lines"]
    assert line["procedure_code"] == "99203"
    assert line["adjustments"] == [
        {"group_code": "CO", "reason_code": "50", "amount": "200.00", "claim_level": False}
    ]
    with session_scope() as session:
        recorded = session.scalar(select(DenialClassification))
    assert recorded is not None
    assert line["classification"] == {
        "category": recorded.category,
        "preventability": recorded.preventability,
        "group_code": "CO",
        "reason_code": "50",
        "taxonomy_version": recorded.taxonomy_version,
    }
    assert line["guidance"]["category"] == recorded.category
    assert line["guidance"]["text"]


@pytest.mark.invariant
def test_similar_denials_and_how_they_ended_are_shown(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """UR-11: the base rate is on screen by default. Two earlier denials of
    the same payer, CPT and code; one was written off."""
    first = denied(root, "CLM-S1", "0001")
    denied(root, "CLM-S2", "0002")
    with session_scope() as session:
        queue.auto_resolve(
            session,
            work_item_id=first,
            client_id=CLIENT_A,
            outcome=WorkItemOutcome.WRITTEN_OFF,
            now=NOW,
        )
    latest = denied(root, "CLM-S3", "0003")
    (line,) = workbench(api, latest).json()["lines"]
    similar = line["similar"]
    assert similar["denials"] == 2  # the other two, not this one
    assert similar["outcomes"] == {"WRITTEN_OFF": 1}
    assert similar["window_days"] == 365
