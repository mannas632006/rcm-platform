"""Prioritising work items and reading the queue (M3.6).

    PD-1: Every decision writes a decision_record with logic version,
    inputs, output.
    PD-15: Timely-filing deadline overrides all other prioritisation.
    FR-WORK-06 (SHOULD): The queue exposes why an item ranks where it does.

`rcm.decision.prioritisation` decides; this module gathers the facts the
decision needs, records it, and stores the result on the item.

**The deadline is the mart's definition, as of today.** The mart computes
`days_to_filing_deadline` as of the submission date, because that is what
was knowable when a claim was sent (PD-11). The queue asks the same question
as of now: `timely_filing_days - (today - date_of_service)`. Same formula,
different clock -- so the queue and the training data agree about what a
deadline is.

**An unknown deadline asks the user** (owner decision, 2026-11-01). The item
is pinned as urgent, and one MISSING_FILING_DEADLINE exception per payer
asks for the term, explaining what happens until it is entered.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.decision.prioritisation import PriorityInputs, PriorityResult, prioritise
from rcm.domain.claim import Claim
from rcm.domain.payer import Payer
from rcm.domain.remittance import ClaimPayment
from rcm.platform.audit import DecisionType, record_decision
from rcm.platform.config import get_settings
from rcm.platform.errors import ResourceNotFoundError, ValidationFailedError
from rcm.platform.logging import get_logger
from rcm.work import exceptions
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import WorkItemState

log = get_logger(__name__)


def outstanding_balance(session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID) -> Decimal:
    """What is still owed on a claim: billed, less everything paid.

    Payments join on CLP01, the submitter's claim number the payer echoes.
    Reversals carry negative amounts, so a clawed-back payment adds back to
    the balance on its own. Never below zero.
    """
    claim = session.scalar(
        select(Claim).where(Claim.claim_id == claim_id, Claim.client_id == client_id)
    )
    if claim is None:
        raise ResourceNotFoundError(entity="claim", entity_id=str(claim_id))
    paid = session.scalar(
        select(func.coalesce(func.sum(ClaimPayment.total_paid), 0)).where(
            ClaimPayment.client_id == client_id,
            ClaimPayment.claim_id == claim.submitter_claim_id,
        )
    )
    # coalesce makes NULL impossible; the guard is for the type checker.
    return max(Decimal("0.00"), claim.total_charge - Decimal(paid if paid is not None else 0))


def _days_to_deadline(
    session: Session, *, item: WorkItem, claim: Claim, now: datetime
) -> int | None:
    """Days left to file, or None -- and if None, ask a person why."""
    payer = session.get(Payer, claim.payer_id)
    term = payer.timely_filing_days if payer else None

    if term is None:
        exceptions.raise_exception(
            session,
            client_id=item.client_id,
            kind=WorkItemKind.MISSING_FILING_DEADLINE,
            problem="No timely-filing deadline is recorded for this claim's payer",
            record=exceptions.RecordRef("claim", claim.claim_id),
            # One question per payer: every claim for it is waiting on the
            # same answer.
            source_key=f"missing_deadline:{claim.payer_id}",
            now=now,
        )
        return None
    if claim.date_of_service is None:
        exceptions.raise_exception(
            session,
            client_id=item.client_id,
            kind=WorkItemKind.DATA_QUALITY,
            problem="Claim has no date of service, so its filing deadline cannot be computed",
            record=exceptions.RecordRef("claim", claim.claim_id),
            source_key=f"no_date_of_service:{claim.claim_id}",
            claim_id=claim.claim_id,
            now=now,
        )
        return None
    return term - (now.date() - claim.date_of_service).days


def prioritise_item(
    session: Session, *, work_item_id: uuid.UUID, client_id: uuid.UUID, now: datetime
) -> PriorityResult:
    """Score or pin one item, record why, and store the result on it."""
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    item = session.scalar(
        select(WorkItem)
        .where(WorkItem.work_item_id == work_item_id, WorkItem.client_id == client_id)
        .with_for_update()
    )
    if item is None:
        raise ResourceNotFoundError(entity="work_item", entity_id=str(work_item_id))

    balance = Decimal("0.00")
    days: int | None = None
    claim: Claim | None = None
    if item.claim_id is not None:
        claim = session.scalar(
            select(Claim).where(Claim.claim_id == item.claim_id, Claim.client_id == client_id)
        )
        if claim is None:
            raise ResourceNotFoundError(entity="claim", entity_id=str(item.claim_id))
        balance = outstanding_balance(session, claim_id=claim.claim_id, client_id=client_id)
        days = _days_to_deadline(session, item=item, claim=claim, now=now)

    inputs = PriorityInputs(
        kind=item.kind,
        balance=balance,
        days_to_deadline=days,
        age_days=max(0, (now - item.created_at).days),
        has_claim=claim is not None,
    )
    settings = get_settings()
    result = prioritise(
        inputs,
        escalation_days=settings.timely_filing_escalation_days,
        bulk_threshold=settings.bulk_handling_threshold,
    )

    item.priority_score = result.score
    item.deadline_pinned = result.pinned
    item.bulk_handling = result.bulk
    item.priority_reason = result.reason
    item.priority_components = result.components
    item.priority_logic_version = result.logic_version
    item.prioritised_at = now

    record_decision(
        session,
        entity_type="work_item",
        entity_id=item.work_item_id,
        decision_type=DecisionType.PRIORITISE,
        decided_by="rules_engine:prioritisation",
        logic_version=result.logic_version,
        input_snapshot={
            "kind": inputs.kind,
            "balance": str(inputs.balance),
            "days_to_deadline": inputs.days_to_deadline,
            "age_days": inputs.age_days,
            "has_claim": inputs.has_claim,
            "escalation_days": settings.timely_filing_escalation_days,
            "bulk_threshold": str(settings.bulk_handling_threshold),
        },
        output={
            "score": result.score,
            "pinned": result.pinned,
            "bulk": result.bulk,
            "reason": result.reason,
        },
    )
    session.flush()
    log.info("work_item.prioritised", pinned=result.pinned, score=result.score)
    return result


def next_items(
    session: Session, *, client_id: uuid.UUID, limit: int = 50, include_bulk: bool = False
) -> list[WorkItem]:
    """The queue, best first: pinned, then score, then SLA, then age.

    PD-15 lives in the first sort key. Bulk work (BR-M10-06) is left out of
    the individual queue unless asked for.
    """
    query = select(WorkItem).where(
        WorkItem.client_id == client_id, WorkItem.state == str(WorkItemState.QUEUED)
    )
    if not include_bulk:
        query = query.where(WorkItem.bulk_handling.is_(False))
    return list(
        session.scalars(
            query.order_by(
                WorkItem.deadline_pinned.desc(),
                WorkItem.priority_score.desc(),
                WorkItem.sla_due_at,
                WorkItem.created_at,
                WorkItem.work_item_id,
            ).limit(limit)
        ).all()
    )
