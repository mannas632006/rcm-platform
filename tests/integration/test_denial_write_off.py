"""Writing off a denial (M5.7 slice 1, BR-M11-05, UR-37).

Moves money, so the rules are tested first:
- a reason is required, and "other" needs words (FR-WORK-04);
- above [TBD-48] a specialist's write-off waits for a *named* approver, who
  may not be the requester (BR-M11-05, SRS 11.5);
- only the payer's share is written off -- the patient's cost share is not
  forgiven by a denial write-off;
- the claim, the work item, the request and the decision move together or
  not at all; a retry with the same key writes off once (PD-14).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rcm.domain.claim import Claim
from rcm.domain.claim_state import ClaimState
from rcm.platform.audit import AuditEvent
from rcm.platform.auth import Principal, Role
from rcm.platform.config import get_settings
from rcm.platform.db import session_scope
from rcm.platform.errors import (
    AuthorizationDeniedError,
    StateTransitionError,
    ValidationFailedError,
)
from rcm.work import dispositions, queue
from rcm.work.dispositions import WriteOff, WriteOffReason, WriteOffStatus
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_acknowledgement_loading import (
    CHARGE,
    CLIENT_A,
    _remittance,
    ack_277ca,
    acknowledge,
    remit,
    submit,
)
from tests.integration.test_api_shell import api, bearer, clean  # noqa: F401
from tests.integration.test_claim_progression import advance
from tests.integration.test_denial_classification import NOW, route

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


@pytest.fixture
def threshold(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A [TBD-48] above this claim's 150.00, to show the no-approval path."""
    monkeypatch.setenv("RCM_WRITE_OFF_APPROVAL_THRESHOLD", "1000.00")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("RCM_WRITE_OFF_APPROVAL_THRESHOLD")
    get_settings.cache_clear()


def person(subject: str, role: Role) -> Principal:
    return Principal(subject=subject, roles=frozenset({role}), client_ids=frozenset({CLIENT_A}))


SPECIALIST = person("den.dana", Role.DENIAL_SPECIALIST)
MANAGER = person("ops.omar", Role.OPERATIONS_MANAGER)


def denied(root: Path, submitter_id: str = "CLM-WO") -> uuid.UUID:
    """Charged 200, paid nothing: CO-50 150.00 the payer refused, PR-1 50.00
    the patient's deductible. Its denial work item id."""
    submit(submitter_id, root, control="0001")
    acknowledge(ack_277ca((submitter_id, "A2:20")), root, name="277ca.edi")
    body = [
        f"CLP*{submitter_id}*4*{CHARGE}*0.00*50.00*MB*PCN{submitter_id}*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*0.00**1~",
        "CAS*CO*50*150.00~",
        "CAS*PR*1*50.00~",
    ]
    remit(_remittance(body, control="0001", check=Decimal("0.00")), root, name="wo.835")
    advance()
    route()
    with session_scope() as session:
        item = session.scalar(
            select(WorkItem.work_item_id).where(WorkItem.kind == str(WorkItemKind.DENIAL))
        )
    assert item is not None
    return item


def write_off(
    item: uuid.UUID,
    who: Principal = SPECIALIST,
    reason: WriteOffReason = WriteOffReason.APPEAL_NOT_VIABLE,
    note: str = "",
) -> dispositions.WriteOffResult:
    with session_scope() as session:
        return dispositions.request_write_off(
            session, principal=who, work_item_id=item, reason=reason, note=note, now=NOW
        )


def decide(write_off_id: uuid.UUID, who: Principal, *, approve: bool) -> WriteOffStatus:
    with session_scope() as session:
        return dispositions.decide_write_off(
            session,
            principal=who,
            write_off_id=write_off_id,
            approve=approve,
            note="checked the record",
            now=NOW,
        )


def claim_state() -> str:
    with session_scope() as session:
        state = session.scalar(select(Claim.state).where(Claim.submitter_claim_id == "CLM-WO"))
    assert state is not None
    return state


def item_state(item: uuid.UUID) -> tuple[str, str | None]:
    with session_scope() as session:
        found = session.get(WorkItem, item)
        assert found is not None
        return found.state, found.outcome


@pytest.mark.usefixtures("threshold")
def test_a_write_off_within_the_threshold_applies_at_once(root: Path) -> None:
    item = denied(root)
    result = write_off(item)
    assert result.status is WriteOffStatus.APPLIED
    assert claim_state() == str(ClaimState.WRITTEN_OFF)
    assert item_state(item) == ("Resolved", "WRITTEN_OFF")


@pytest.mark.invariant
def test_a_write_off_never_forgives_the_patients_share(root: Path) -> None:
    """Only the payer's refusal is written off: CO-50's 150.00, not the
    PR-1 deductible the patient still owes."""
    result = write_off(denied(root))
    assert result.amount == Decimal("150.00")


@pytest.mark.invariant
def test_a_write_off_above_the_threshold_waits_for_named_approval(root: Path) -> None:
    """BR-M11-05. The placeholder [TBD-48] is 0.00: until the owner sets it,
    every specialist write-off waits -- the closed side of an open decision."""
    item = denied(root)
    result = write_off(item)
    assert result.status is WriteOffStatus.PENDING_APPROVAL
    assert claim_state() == str(ClaimState.DENIED)
    state, outcome = item_state(item)
    assert outcome is None
    assert state != "Resolved"


@pytest.mark.invariant
def test_the_requester_cannot_approve_their_own_write_off(root: Path) -> None:
    result = write_off(denied(root))
    same_person = person(SPECIALIST.subject, Role.OPERATIONS_MANAGER)
    with pytest.raises(AuthorizationDeniedError):
        decide(result.write_off_id, same_person, approve=True)
    assert claim_state() == str(ClaimState.DENIED)


def test_a_specialist_cannot_approve_a_write_off(root: Path) -> None:
    result = write_off(denied(root))
    with pytest.raises(AuthorizationDeniedError):
        decide(result.write_off_id, person("den.other", Role.DENIAL_SPECIALIST), approve=True)


def test_an_approved_write_off_is_applied(root: Path) -> None:
    item = denied(root)
    result = write_off(item)
    assert decide(result.write_off_id, MANAGER, approve=True) is WriteOffStatus.APPLIED
    assert claim_state() == str(ClaimState.WRITTEN_OFF)
    assert item_state(item) == ("Resolved", "WRITTEN_OFF")


def test_a_rejected_write_off_leaves_the_denial_to_work(root: Path) -> None:
    item = denied(root)
    result = write_off(item)
    assert decide(result.write_off_id, MANAGER, approve=False) is WriteOffStatus.REJECTED
    assert claim_state() == str(ClaimState.DENIED)
    assert item_state(item)[1] is None


def test_a_write_off_is_decided_once(root: Path) -> None:
    result = write_off(denied(root))
    decide(result.write_off_id, MANAGER, approve=False)
    with pytest.raises(StateTransitionError):
        decide(result.write_off_id, MANAGER, approve=True)


def test_a_manager_writes_off_any_amount_without_waiting(root: Path) -> None:
    """SRS 11.5: an Ops Manager's write-off limit is "Any"."""
    result = write_off(denied(root), who=MANAGER)
    assert result.status is WriteOffStatus.APPLIED


def test_other_needs_words(root: Path) -> None:
    """FR-WORK-04: a structured reason; "other" must say what."""
    with pytest.raises(ValidationFailedError):
        write_off(denied(root), reason=WriteOffReason.OTHER, note="  ")


@pytest.mark.usefixtures("threshold")
def test_a_claim_written_off_cannot_be_written_off_again(root: Path) -> None:
    item = denied(root)
    write_off(item)
    with pytest.raises(StateTransitionError):
        write_off(item)


def test_a_second_request_while_one_waits_is_refused(root: Path) -> None:
    item = denied(root)
    write_off(item)
    with pytest.raises(StateTransitionError):
        write_off(item)


# --- through the API -------------------------------------------------------


def post_disposition(
    api: TestClient,  # noqa: F811
    item: uuid.UUID,
    key: str,
    role: Role = Role.DENIAL_SPECIALIST,
    subject: str = "den.dana",
) -> Any:
    return api.post(
        f"/api/v1/denials/{item}/disposition",
        json={"action": "WRITE_OFF", "reason": "APPEAL_NOT_VIABLE", "note": ""},
        headers={**bearer(CLIENT_A, role=role, subject=subject), "Idempotency-Key": key},
    )


@pytest.mark.invariant
@pytest.mark.usefixtures("threshold")
def test_a_retried_write_off_writes_off_once(api: TestClient, root: Path) -> None:  # noqa: F811
    """PD-14: same key, one effect -- and the same answer."""
    item = denied(root)
    # Unique per run: idempotency keys live in Redis across test runs.
    key = f"wo-{uuid.uuid4()}"
    first = post_disposition(api, item, key)
    again = post_disposition(api, item, key)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    with session_scope() as session:
        assert len(session.scalars(select(WriteOff)).all()) == 1


def test_a_write_off_needs_denial_disposition(api: TestClient, root: Path) -> None:  # noqa: F811
    item = denied(root)
    assert (
        post_disposition(api, item, f"wo-{uuid.uuid4()}", role=Role.RULES_AUTHOR).status_code == 403
    )


@pytest.mark.usefixtures("threshold")
def test_a_write_off_is_audited_with_its_amount(api: TestClient, root: Path) -> None:  # noqa: F811
    """S-07: who wrote off how much, and why -- in the same transaction."""
    item = denied(root)
    subject = f"den.{uuid.uuid4().hex[:8]}"
    assert post_disposition(api, item, f"wo-{uuid.uuid4()}", subject=subject).status_code == 200
    with session_scope() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.actor_id == subject, AuditEvent.action == "write_off.requested"
            )
        ).all()
    (event,) = events
    assert event.after_state is not None
    assert event.after_state["amount"] == "150.00"
    assert event.after_state["reason"] == "APPEAL_NOT_VIABLE"


def test_the_workbench_shows_a_write_off_waiting_for_approval(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """The page must say a write-off waits, after a reload too -- otherwise a
    second person asks again, or the denial looks untouched."""
    item = denied(root)
    asked = write_off(item)
    body = api.get(
        f"/api/v1/denials/{item}", headers=bearer(CLIENT_A, role=Role.DENIAL_SPECIALIST)
    ).json()
    assert body["write_offs"] == [
        {
            "write_off_id": str(asked.write_off_id),
            "status": "PENDING_APPROVAL",
            "amount": "150.00",
            "reason": "APPEAL_NOT_VIABLE",
            "note": "",
            "requested_by": SPECIALIST.subject,
            "requested_at": body["write_offs"][0]["requested_at"],
            "decided_by": None,
        }
    ]
    decide(asked.write_off_id, MANAGER, approve=False)
    body = api.get(
        f"/api/v1/denials/{item}", headers=bearer(CLIENT_A, role=Role.DENIAL_SPECIALIST)
    ).json()
    assert body["write_offs"][0]["status"] == "REJECTED"
    assert body["write_offs"][0]["decided_by"] == MANAGER.subject


def test_a_differently_written_name_is_still_the_requester(root: Path) -> None:
    """BR-M11-05: "DEN.Dana " is den.dana; they still cannot approve."""
    result = write_off(denied(root))
    same_person = person(" DEN.Dana ", Role.OPERATIONS_MANAGER)
    with pytest.raises(AuthorizationDeniedError):
        decide(result.write_off_id, same_person, approve=True)


def test_an_approver_can_decide_while_the_requester_held_the_item(root: Path) -> None:
    """A waiting write-off frees the item, so approval is not blocked by the
    requester's lease."""
    item = denied(root)
    with session_scope() as session:
        queue.claim(
            session, work_item_id=item, client_id=CLIENT_A, operator=SPECIALIST.subject, now=NOW
        )
    result = write_off(item)
    assert decide(result.write_off_id, MANAGER, approve=True) is WriteOffStatus.APPLIED
