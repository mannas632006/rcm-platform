"""What an operator decides to do with a denial (M5.7, S-04).

This slice holds the write-off (BR-M11-05). It moves money, so every step
runs in the caller's transaction -- the request, the approval, the claim's
move to WrittenOff and the work item's resolution land together or not at
all -- and each writes an audit event (S-07).

**Only the payer's share is written off.** The amount is the denied lines'
adjustments outside the PR group. A patient's cost share is not forgiven by
writing off a denial; that is a different, regulated act.

**Above [TBD-48], a named approver.** SRS 11.5 gives specialists a write-off
limit of "<= threshold" and an Ops Manager "Any". A request above the limit
waits for someone holding `write_off:approve` who is not the requester. The
placeholder threshold is 0.00, so until the owner sets it every specialist
write-off waits: the closed side of an open decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.decision.denial_models import DenialClassification
from rcm.decision.denials import TAXONOMY_VERSION
from rcm.domain.claim import Claim
from rcm.domain.claim_lifecycle import transition
from rcm.domain.claim_state import ClaimState, Guard
from rcm.domain.remittance import RemittanceAdjustment
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.auth import Permission, Principal, scoped_query
from rcm.platform.config import get_settings
from rcm.platform.errors import (
    AuthorizationDeniedError,
    ResourceNotFoundError,
    StateTransitionError,
    ValidationFailedError,
)
from rcm.work import queue
from rcm.work.denial_workbench import DENIAL_KEY
from rcm.work.models import (
    WorkItem,
    WorkItemKind,
    WriteOff,
    WriteOffDecision,
    WriteOffDecisionKind,
    WriteOffReason,
)
from rcm.work.state import OPEN_STATES, WorkItemOutcome, WorkItemState

__all__ = [
    "PatientShareResult",
    "WriteOff",
    "WriteOffReason",
    "WriteOffResult",
    "WriteOffStatus",
    "bill_patient",
    "decide_write_off",
    "request_write_off",
]


#: CAS group "patient responsibility" (X12). Everything else is the payer's.
PATIENT_GROUP = "PR"


class WriteOffStatus(StrEnum):
    APPLIED = "APPLIED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class WriteOffResult:
    write_off_id: uuid.UUID
    status: WriteOffStatus
    amount: Decimal
    threshold: Decimal


def _denial_item(session: Session, *, principal: Principal, work_item_id: uuid.UUID) -> WorkItem:
    item = session.scalar(
        scoped_query(
            select(WorkItem)
            .where(
                WorkItem.work_item_id == work_item_id,
                WorkItem.kind == str(WorkItemKind.DENIAL),
            )
            .with_for_update(),
            WorkItem,
            principal,
        )
    )
    if item is None or not (item.source_key or "").startswith(DENIAL_KEY):
        raise ResourceNotFoundError(entity="denial", entity_id=str(work_item_id))
    if WorkItemState(item.state) not in OPEN_STATES:
        # A finished item is history, not a handle on the claim: the claim
        # may since have been denied again under another payment.
        raise StateTransitionError(
            source=item.state, target="disposition", reason="this denial is already resolved"
        )
    return item


def _same_person(a: str, b: str) -> bool:
    """BR-M11-05 compares people, not spellings of a subject."""
    return a.strip().casefold() == b.strip().casefold()


def _payer_share(session: Session, *, item: WorkItem) -> Decimal:
    """The denied lines' adjustments outside the patient's group (PR)."""
    return _denied_share(session, item=item, patient=False)


def _patient_share(session: Session, *, item: WorkItem) -> Decimal:
    """The denied lines' PR adjustments -- the only part a patient may owe."""
    return _denied_share(session, item=item, patient=True)


def _denied_share(session: Session, *, item: WorkItem, patient: bool) -> Decimal:
    payment_id = uuid.UUID((item.source_key or "").removeprefix(DENIAL_KEY))
    total = session.scalar(
        select(func.coalesce(func.sum(RemittanceAdjustment.amount), 0))
        .join(
            DenialClassification,
            DenialClassification.service_line_id == RemittanceAdjustment.service_line_id,
        )
        .where(
            RemittanceAdjustment.client_id == item.client_id,
            RemittanceAdjustment.claim_payment_id == payment_id,
            (RemittanceAdjustment.cas_group_code == PATIENT_GROUP)
            if patient
            else (RemittanceAdjustment.cas_group_code != PATIENT_GROUP),
            DenialClassification.client_id == item.client_id,
            DenialClassification.taxonomy_version == TAXONOMY_VERSION,
        )
    )
    return Decimal(total if total is not None else 0).quantize(Decimal("0.01"))


def _waiting(session: Session, *, item: WorkItem) -> WriteOff | None:
    return session.scalar(
        select(WriteOff)
        .outerjoin(WriteOffDecision, WriteOffDecision.write_off_id == WriteOff.write_off_id)
        .where(
            WriteOff.client_id == item.client_id,
            WriteOff.work_item_id == item.work_item_id,
            WriteOff.needs_approval.is_(True),
            WriteOffDecision.write_off_id.is_(None),
        )
    )


def request_write_off(
    session: Session,
    *,
    principal: Principal,
    work_item_id: uuid.UUID,
    reason: WriteOffReason,
    note: str,
    now: datetime,
) -> WriteOffResult:
    """Ask to write off a denial's payer share; apply it at once if within
    the asker's limit, otherwise leave it for a named approver."""
    principal.require(Permission.DENIAL_DISPOSITION)
    item = _denial_item(session, principal=principal, work_item_id=work_item_id)
    if reason is WriteOffReason.OTHER and not note.strip():
        raise ValidationFailedError(field="note", reason='a write-off for "other" must say why')
    if item.claim_id is None:
        raise ValidationFailedError(
            field="work_item_id",
            reason="the denied payment names no claim on file; find the claim first",
        )
    if _waiting(session, item=item) is not None:
        raise StateTransitionError(
            source="write-off awaiting approval",
            target="write-off",
            reason="a write-off of this denial is already waiting for approval",
        )
    state = session.scalar(
        select(Claim.state).where(
            Claim.client_id == item.client_id, Claim.claim_id == item.claim_id
        )
    )
    if state != str(ClaimState.DENIED):
        raise StateTransitionError(
            source=str(state),
            target=str(ClaimState.WRITTEN_OFF),
            reason="only a denied claim is written off from the Denial Workbench",
        )
    amount = _payer_share(session, item=item)
    if amount <= 0:
        raise ValidationFailedError(
            field="work_item_id", reason="this denial has no payer share to write off"
        )

    threshold = get_settings().write_off_approval_threshold
    needs_approval = amount > threshold and not principal.has(Permission.WRITE_OFF_APPROVE)
    write_off = WriteOff(
        client_id=item.client_id,
        work_item_id=item.work_item_id,
        claim_id=item.claim_id,
        amount=amount,
        reason=str(reason),
        note=note.strip(),
        requested_by=principal.subject,
        requested_at=now,
        needs_approval=needs_approval,
        threshold=threshold,
    )
    session.add(write_off)
    session.flush()
    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        action="write_off.requested",
        entity_type="write_off",
        entity_id=write_off.write_off_id,
        after_state={
            "work_item_id": str(item.work_item_id),
            "amount": str(amount),
            "reason": str(reason),
            "needs_approval": needs_approval,
            "threshold": str(threshold),
        },
    )
    if needs_approval:
        if item.assigned_to is not None and _same_person(item.assigned_to, principal.subject):
            # Waiting for someone else: free the item, so the requester's
            # lease cannot block the approver.
            queue.release(
                session,
                work_item_id=item.work_item_id,
                client_id=item.client_id,
                operator=item.assigned_to,
                now=now,
            )
        return WriteOffResult(
            write_off.write_off_id, WriteOffStatus.PENDING_APPROVAL, amount, threshold
        )
    _apply(session, write_off=write_off, item=item, operator=principal.subject, now=now)
    return WriteOffResult(write_off.write_off_id, WriteOffStatus.APPLIED, amount, threshold)


@dataclass(frozen=True, slots=True)
class PatientShareResult:
    amount: Decimal


def bill_patient(
    session: Session,
    *,
    principal: Principal,
    work_item_id: uuid.UUID,
    note: str,
    now: datetime,
) -> PatientShareResult:
    """Accept the payer's refusal and move the patient's share to them.

    ADR-0016: Denied -> PatientBalance, guarded on a PR amount on the denied
    lines. The amount is that PR share and nothing else; the CO amount is the
    provider's and never reaches a patient (PD-19).
    """
    principal.require(Permission.DENIAL_DISPOSITION)
    item = _denial_item(session, principal=principal, work_item_id=work_item_id)
    if item.claim_id is None:
        raise ValidationFailedError(
            field="work_item_id",
            reason="the denied payment names no claim on file; find the claim first",
        )
    if _waiting(session, item=item) is not None:
        raise StateTransitionError(
            source="write-off awaiting approval",
            target=str(ClaimState.PATIENT_BALANCE),
            reason="a write-off of this denial is waiting for approval; decide that first",
        )
    amount = _patient_share(session, item=item)
    if amount <= 0:
        raise ValidationFailedError(
            field="work_item_id",
            reason="the denied lines carry no patient-responsibility (PR) amount; "
            "contractual amounts are never billed to the patient",
        )
    payer = _payer_share(session, item=item)
    if payer > 0:
        # Leaving Denied would drop the payer's share with no write-off and
        # no approval (BR-M11-05). Real teams split the two; until patient
        # balances live apart from claim state, refuse (ADR-0016).
        raise StateTransitionError(
            source=str(ClaimState.DENIED),
            target=str(ClaimState.PATIENT_BALANCE),
            reason=f"the denial still carries {payer} the payer refused outside the patient's "
            "share; appeal, correct or write that off first -- it is never billed to the patient",
        )
    transition(
        session,
        claim_id=item.claim_id,
        client_id=item.client_id,
        target=ClaimState.PATIENT_BALANCE,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        facts={str(Guard.PATIENT_SHARE_DENIED): True},
        # The patient statement is the next step, due now.
        next_action_due=now.date(),
        # PD-13: the operator's free text stays on the work item, never in
        # the claim's history.
        note=f"patient share {amount}",
    )
    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        action="denial.billed_to_patient",
        entity_type="claim",
        entity_id=item.claim_id,
        after_state={"amount": str(amount), "work_item_id": str(item.work_item_id)},
    )
    _close(
        session,
        item=item,
        operator=principal.subject,
        outcome=WorkItemOutcome.BILLED_TO_PATIENT,
        note=note.strip(),
        now=now,
    )
    return PatientShareResult(amount=amount)


def decide_write_off(
    session: Session,
    *,
    principal: Principal,
    write_off_id: uuid.UUID,
    approve: bool,
    note: str,
    now: datetime,
) -> WriteOffStatus:
    """A named approver's one decision on a write-off that needed it."""
    principal.require(Permission.WRITE_OFF_APPROVE)
    write_off = session.scalar(
        scoped_query(
            select(WriteOff).where(WriteOff.write_off_id == write_off_id), WriteOff, principal
        )
    )
    if write_off is None:
        raise ResourceNotFoundError(entity="write_off", entity_id=str(write_off_id))
    # Serialise on the denial's work item, not the write-off: the write-off
    # tables are insert-only, and FOR UPDATE needs the UPDATE grant they
    # deliberately lack. Every decision on a denial takes this lock first.
    item = _denial_item(session, principal=principal, work_item_id=write_off.work_item_id)
    if _same_person(write_off.requested_by, principal.subject):
        # BR-M11-05: approval is a second person's. A CHECK cannot see both
        # tables, so it is refused here.
        raise AuthorizationDeniedError()
    if not write_off.needs_approval:
        raise StateTransitionError(
            source="applied", target="decided", reason="this write-off needed no approval"
        )
    if session.get(WriteOffDecision, write_off_id) is not None:
        raise StateTransitionError(
            source="decided", target="decided", reason="this write-off is already decided"
        )

    decision = WriteOffDecisionKind.APPROVED if approve else WriteOffDecisionKind.REJECTED
    session.add(
        WriteOffDecision(
            write_off_id=write_off_id,
            client_id=write_off.client_id,
            decision=str(decision),
            decided_by=principal.subject,
            decided_at=now,
            note=note.strip(),
        )
    )
    session.flush()
    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        action=f"write_off.{decision.lower()}",
        entity_type="write_off",
        entity_id=write_off_id,
        before_state={"requested_by": write_off.requested_by, "amount": str(write_off.amount)},
        after_state={"decision": str(decision)},
    )
    if not approve:
        return WriteOffStatus.REJECTED
    _apply(session, write_off=write_off, item=item, operator=principal.subject, now=now)
    return WriteOffStatus.APPLIED


def _apply(
    session: Session, *, write_off: WriteOff, item: WorkItem, operator: str, now: datetime
) -> None:
    """Move the claim to WrittenOff and close the denial, as `operator`.

    The item is taken for `operator` if nobody holds it; if someone else
    does, `queue.claim` refuses by name and the whole transaction -- request
    and decision included -- rolls back.
    """
    transition(
        session,
        claim_id=write_off.claim_id,
        client_id=write_off.client_id,
        target=ClaimState.WRITTEN_OFF,
        actor_type=ActorType.USER,
        actor_id=operator,
        # Closing the written-off claim is the next step, due now.
        next_action_due=now.date(),
        note=f"write-off {write_off.reason}: {write_off.amount}",
    )
    _close(
        session,
        item=item,
        operator=operator,
        outcome=WorkItemOutcome.WRITTEN_OFF,
        note=write_off.reason,
        now=now,
    )


def _close(
    session: Session,
    *,
    item: WorkItem,
    operator: str,
    outcome: WorkItemOutcome,
    note: str,
    now: datetime,
) -> None:
    """Resolve the denial item as `operator`, taking it first if nobody holds
    it. Held by someone else, `queue.claim` refuses by name and the caller's
    whole transaction rolls back."""
    if item.assigned_to != operator:
        queue.claim(
            session,
            work_item_id=item.work_item_id,
            client_id=item.client_id,
            operator=operator,
            now=now,
        )
    if item.state == str(WorkItemState.ASSIGNED):
        # Deciding is working the item: Assigned -> InProgress -> Resolved.
        queue.open_item(
            session,
            work_item_id=item.work_item_id,
            client_id=item.client_id,
            operator=operator,
            now=now,
        )
    queue.resolve(
        session,
        work_item_id=item.work_item_id,
        client_id=item.client_id,
        operator=operator,
        outcome=outcome,
        now=now,
        note=note,
    )
