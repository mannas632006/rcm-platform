"""S-04 Denial Workbench, read side (M5.7 slice 1).

Through the real app, loaders, progression and denial routing. A denial is
a PHI read: scope (PD-20) and audit (PD-2) first. Then what the operator
needs to decide: each denied line with group and reason together (PD-3),
its category and preventability as classified (BR-TAX-01), and how the
same denial went before (UR-11).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rcm.decision.denial_models import DenialClassification
from rcm.domain.remittance import ClaimPayment
from rcm.platform.audit import AuditEvent
from rcm.platform.auth import Role
from rcm.platform.db import session_scope
from rcm.work import denials as work_denials
from rcm.work import queue
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import WorkItemOutcome
from tests.integration.test_acknowledgement_loading import (
    CHARGE,
    CLIENT_A,
    CLIENT_B,
    _remittance,
    denial_for,
    remit,
    submit,
)
from tests.integration.test_api_shell import api, bearer, clean, raise_item  # noqa: F401
from tests.integration.test_claim_progression import resubmit
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


def test_similar_denials_count_only_the_year_before_this_one(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """The window is the year before this denial's 835, by payment date --
    not by when the classifier happened to run."""
    submit("CLM-OLD", root, control="0001")
    old = denial_for("CLM-OLD", control="0001").replace(b"20240131", b"20220131")
    remit(old, root, name="old.835")
    route()
    latest = denied(root, "CLM-NEW", "0002")
    (line,) = workbench(api, latest).json()["lines"]
    assert line["similar"]["denials"] == 0


def test_a_resubmission_is_not_its_own_similar_denial(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """PD-7: the original's denial is the same claim, not another one."""
    denied(root, "CLM-O", "0001")
    resubmit("CLM-O2", "CLM-O", root, frequency="7")
    remit(denial_for("CLM-O2", control="0002"), root, name="o2.835")
    route()
    with session_scope() as session:
        payment = session.scalar(
            select(ClaimPayment.claim_payment_id).where(ClaimPayment.claim_id == "CLM-O2")
        )
        item = session.scalar(
            select(WorkItem.work_item_id).where(WorkItem.source_key == f"denial:{payment}")
        )
    assert item is not None
    (line,) = workbench(api, item).json()["lines"]
    assert line["similar"]["denials"] == 0


@pytest.mark.invariant
def test_a_denied_line_not_yet_classified_is_not_shown_as_paid(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """PD-18: a refusal outside the taxonomy is a denial still waiting for a
    category -- never a line the payer paid."""
    submit("CLM-MX", root, control="0001")
    body = [
        f"CLP*CLM-MX*4*{2 * CHARGE}*0.00**MB*PCNCLM-MX*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*0.00**1~",
        f"CAS*CO*50*{CHARGE}~",
        f"SVC*HC:99204*{CHARGE}*0.00**1~",
        f"CAS*CO*A1*{CHARGE}~",
    ]
    remit(_remittance(body, control="0001", check=Decimal("0.00")), root, name="mx.835")
    route()
    with session_scope() as session:
        item = session.scalar(
            select(WorkItem.work_item_id).where(WorkItem.kind == str(WorkItemKind.DENIAL))
        )
    assert item is not None
    lines = {line["procedure_code"]: line for line in workbench(api, item).json()["lines"]}
    assert lines["99203"]["status"] == "DENIED"
    assert lines["99204"]["status"] == "UNCLASSIFIED"
    assert lines["99204"]["classification"] is None


def test_claim_level_adjustments_are_shown(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """PD-6: a claim-level CAS is an adjustment like any other. Each line
    balances on its own; the claim pays its lines less the OA-23."""
    submit("CLM-CL", root, control="0001")
    body = [
        "CLP*CLM-CL*2*400.00*80.00**MB*PCNCLM-CL*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        "CAS*OA*23*20.00~",
        "SVC*HC:99213*200.00*100.00**1~",
        "CAS*CO*45*100.00~",
        "SVC*HC:99203*200.00*0.00**1~",
        "CAS*CO*50*200.00~",
    ]
    remit(_remittance(body, control="0001", check=Decimal("80.00")), root, name="cl.835")
    route()
    with session_scope() as session:
        item = session.scalar(
            select(WorkItem.work_item_id).where(WorkItem.kind == str(WorkItemKind.DENIAL))
        )
    assert item is not None
    found = workbench(api, item).json()
    assert found["claim_adjustments"] == [
        {"group_code": "OA", "reason_code": "23", "amount": "20.00", "claim_level": True}
    ]
    statuses = {line["procedure_code"]: line["status"] for line in found["lines"]}
    assert statuses == {"99213": "NOT_DENIED", "99203": "DENIED"}


def test_a_resolved_denial_is_never_raised_again(root: Path) -> None:
    """Even if its payment is classified afresh (a new taxonomy version),
    a denial a person has finished does not come back."""
    item = denied(root, "CLM-RR", "0001")
    with session_scope() as session:
        queue.auto_resolve(
            session,
            work_item_id=item,
            client_id=CLIENT_A,
            outcome=WorkItemOutcome.WRITTEN_OFF,
            now=NOW,
        )
        payment = session.scalar(
            select(ClaimPayment.claim_payment_id).where(ClaimPayment.claim_id == "CLM-RR")
        )
    assert payment is not None
    with session_scope() as session:
        work_denials._raise_denial(session, client_id=CLIENT_A, claim_payment_id=payment, now=NOW)
        items = session.scalars(
            select(WorkItem.work_item_id).where(WorkItem.kind == str(WorkItemKind.DENIAL))
        ).all()
    assert items == [item]
