"""A denial's patient share billed to the patient (M5.7, ADR-0016, PD-19).

The owner chose (2026-11-06) to allow Denied -> PatientBalance only for the
amount the payer put in the patient-responsibility group (PR). Contractual
(CO) amounts are the provider's and are never billed to a patient (PD-19).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rcm.domain.claim import Claim
from rcm.domain.claim_state import ClaimState
from rcm.platform.audit import AuditEvent
from rcm.platform.auth import Role
from rcm.platform.db import session_scope
from rcm.platform.errors import (
    AuthorizationDeniedError,
    StateTransitionError,
    ValidationFailedError,
)
from rcm.work import dispositions
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_acknowledgement_loading import (
    CLIENT_A,
    ack_277ca,
    acknowledge,
    denial_for,
    remit,
    submit,
)
from tests.integration.test_api_shell import api, bearer, clean  # noqa: F401
from tests.integration.test_claim_progression import advance
from tests.integration.test_denial_classification import NOW, route
from tests.integration.test_denial_write_off import MANAGER, SPECIALIST, denied, person

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def bill(item: uuid.UUID, who: Any = SPECIALIST) -> dispositions.PatientShareResult:
    with session_scope() as session:
        return dispositions.bill_patient(
            session, principal=who, work_item_id=item, note="", now=NOW
        )


def claim_state(submitter_id: str = "CLM-WO") -> str:
    with session_scope() as session:
        state = session.scalar(select(Claim.state).where(Claim.submitter_claim_id == submitter_id))
    assert state is not None
    return state


@pytest.mark.invariant
def test_co_group_never_billed_to_patient(root: Path) -> None:
    """PD-19. The denial carries CO-50 150.00 and PR-1 50.00; the patient is
    billed the 50.00 and never any of the CO amount."""
    item = denied(root)
    result = bill(item)
    assert result.amount == Decimal("50.00")
    assert claim_state() == str(ClaimState.PATIENT_BALANCE)
    with session_scope() as session:
        found = session.get(WorkItem, item)
        assert found is not None
        assert (found.state, found.outcome) == ("Resolved", "BILLED_TO_PATIENT")
        # Audit rows are append-only and outlive test cleanup: this claim's only.
        (event,) = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "denial.billed_to_patient",
                AuditEvent.entity_id == found.claim_id,
            )
        ).all()
    assert event.after_state == {"amount": "50.00", "work_item_id": str(item)}


def test_a_denial_with_no_patient_share_stays_off_the_patient(root: Path) -> None:
    """CO-50 alone: nothing is the patient's, so nothing moves (PD-19)."""
    submit("CLM-CO", root, control="0001")
    acknowledge(ack_277ca(("CLM-CO", "A2:20")), root, name="277ca.edi")
    remit(denial_for("CLM-CO", control="0001"), root, name="co.835")
    advance()
    route()
    with session_scope() as session:
        item = session.scalar(
            select(WorkItem.work_item_id).where(WorkItem.kind == str(WorkItemKind.DENIAL))
        )
    assert item is not None
    with pytest.raises(ValidationFailedError):
        bill(item)
    assert claim_state("CLM-CO") == str(ClaimState.DENIED)


def test_only_a_denied_claim_goes_to_the_patient(root: Path) -> None:
    item = denied(root)
    bill(item)
    with pytest.raises(StateTransitionError):
        bill(item)


def test_billing_the_patient_needs_denial_disposition(root: Path) -> None:
    item = denied(root)
    with pytest.raises(AuthorizationDeniedError):
        bill(item, who=person("rules.ria", Role.RULES_AUTHOR))


def test_a_write_off_waiting_blocks_billing_the_patient(root: Path) -> None:
    """One decision at a time: a write-off waits, so the denial is not also
    sent to the patient behind the approver's back."""
    item = denied(root)
    with session_scope() as session:
        dispositions.request_write_off(
            session,
            principal=SPECIALIST,
            work_item_id=item,
            reason=dispositions.WriteOffReason.APPEAL_NOT_VIABLE,
            note="",
            now=NOW,
        )
    with pytest.raises(StateTransitionError):
        bill(item, who=MANAGER)


@pytest.mark.invariant
def test_billing_the_patient_twice_with_one_key_bills_once(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """PD-14."""
    item = denied(root)
    key = f"pr-{uuid.uuid4()}"
    subject = f"den.{uuid.uuid4().hex[:8]}"
    headers = {
        **bearer(CLIENT_A, role=Role.DENIAL_SPECIALIST, subject=subject),
        "Idempotency-Key": key,
    }
    path = f"/api/v1/denials/{item}/patient-responsibility"
    first = api.post(path, json={"note": ""}, headers=headers)
    again = api.post(path, json={"note": ""}, headers=headers)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json() == {"amount": "50.00"}
    with session_scope() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "denial.billed_to_patient", AuditEvent.actor_id == subject
            )
        ).all()
    assert len(events) == 1
