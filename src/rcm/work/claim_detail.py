"""What S-03 Claim Detail reads: one claim, everything needed to decide (M5.5).

    PD-20: the claim is found through `scoped_query`; every other row is read
    by the claim's own client id, so nothing outside the scope can join in.
    PD-3 / PD-6: adjustments are returned one row each, group and reason
    together -- never a bare CARC, never summed.
    PD-7: a resubmission is the same claim; the whole chain is listed.
    UR-06: every automated decision on the claim, its payments, their lines
    and its work items, with the logic version and inputs.

**The 835 is shown as the payer sent it.** Remittance lines are not matched
to 837 lines here: a payer may split, bundle or renumber lines, and a wrong
match would put one line's denial on another. Matching is M5.7's job, with a
person checking it.

**The member id is masked to its last four** (S-6). Enough to confirm a match
on a payer call; the full id is not needed to work the claim from this
screen.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim, ClaimLine
from rcm.domain.claim_lifecycle import history
from rcm.domain.claim_state import ClaimState
from rcm.domain.payer import Payer
from rcm.domain.remittance import (
    ClaimPayment,
    Remittance,
    RemittanceAdjustment,
    RemittanceServiceLine,
)
from rcm.platform.audit import DecisionRecord
from rcm.platform.auth import Principal, scoped_query
from rcm.platform.errors import ResourceNotFoundError
from rcm.work.models import WorkItem
from rcm.work.priority import FILING_CLOCK_STATES, outstanding_balance

MASK = "•"


class FilingStatus(StrEnum):
    #: The clock is running: `days_left` is set (negative once missed).
    RUNNING = "RUNNING"
    #: The payer took the claim; this clock is over (later ones are M11's).
    FILED = "FILED"
    #: No term recorded for the payer, or no date of service. Never a guess.
    UNKNOWN = "UNKNOWN"
    #: The claim was never filed and no longer will be (cancelled, auth
    #: denied, replaced, written off). Not "filed": that would be untrue.
    NOT_APPLICABLE = "NOT_APPLICABLE"


#: The payer received the claim: everything from acceptance on.
FILED_STATES: frozenset[ClaimState] = frozenset(
    {
        ClaimState.IN_ADJUDICATION,
        ClaimState.PAID,
        ClaimState.PARTIALLY_PAID,
        ClaimState.DENIED,
        ClaimState.APPEALING,
        ClaimState.PATIENT_BALANCE,
        ClaimState.CLOSED,
        ClaimState.REOPENED,
    }
)
#: No filing clock applies. A replaced submission's clock lives on in its
#: replacement; a written-off claim is no longer being pursued.
NO_FILING_STATES: frozenset[ClaimState] = frozenset(
    {
        ClaimState.AUTH_DENIED,
        ClaimState.CANCELLED,
        ClaimState.REPLACED,
        ClaimState.WRITTEN_OFF,
    }
)


@dataclass(frozen=True, slots=True)
class Filing:
    status: FilingStatus
    days_left: int | None


@dataclass(frozen=True, slots=True)
class Line:
    line_number: int
    procedure_code: str
    modifiers: list[str]
    units: Decimal
    charge: Decimal
    service_date: date | None
    diagnosis_pointers: list[int]


@dataclass(frozen=True, slots=True)
class Adjustment:
    group_code: str
    reason_code: str
    amount: Decimal
    claim_level: bool


@dataclass(frozen=True, slots=True)
class PaidLine:
    line_number: int
    procedure_code: str
    modifiers: list[str]
    charge: Decimal
    allowed: Decimal | None
    paid: Decimal
    remark_codes: list[str]
    adjustments: list[Adjustment]


@dataclass(frozen=True, slots=True)
class Payment:
    claim_payment_id: uuid.UUID
    #: Which submission of the claim the payer answered (PD-7).
    submitter_claim_id: str
    attempt: int
    payer_control_number: str
    status_code: str
    payment_date: date | None
    total_charge: Decimal
    total_paid: Decimal
    patient_responsibility: Decimal
    is_reversal: bool
    lines: list[PaidLine]
    claim_adjustments: list[Adjustment]


@dataclass(frozen=True, slots=True)
class ChainLink:
    claim_id: uuid.UUID
    submitter_claim_id: str
    attempt: int
    state: str


@dataclass(frozen=True, slots=True)
class Event:
    occurred_at: datetime
    source_state: str
    target_state: str
    reason: str
    note: str
    actor_type: str
    actor_id: str
    review_status: str


@dataclass(frozen=True, slots=True)
class Decision:
    decision_id: uuid.UUID
    decided_at: datetime
    entity_type: str
    entity_id: uuid.UUID
    decision_type: str
    decided_by: str
    logic_version: str
    inputs: dict[str, Any]
    output: dict[str, Any]
    confidence: float | None


@dataclass(frozen=True, slots=True)
class ClaimDetail:
    claim_id: uuid.UUID
    submitter_claim_id: str
    state: str
    payer_id: str
    payer_name: str | None
    date_of_service: date | None
    submitted_on: date | None
    next_action_due: date | None
    total_charge: Decimal
    balance: Decimal
    member_id_masked: str | None
    place_of_service: str
    diagnoses: list[str]
    filing: Filing
    lines: list[Line]
    payments: list[Payment]
    chain: list[ChainLink]
    timeline: list[Event]
    decisions: list[Decision]


def mask(identifier: str) -> str | None:
    """The last four characters, the rest hidden; short ids hide entirely.
    None when there is no id: a mask would make a missing id look present."""
    identifier = identifier.strip()
    if not identifier:
        return None
    if len(identifier) <= 4:
        return MASK * 4
    return MASK * 4 + identifier[-4:]


def filing(
    state: ClaimState, *, term: int | None, service_date: date | None, today: date
) -> Filing:
    """UR-07's countdown, by the queue's formula (`rcm.work.priority`):
    `term - (today - date of service)`, so the screen and the queue agree."""
    if state in FILED_STATES:
        return Filing(FilingStatus.FILED, None)
    if state not in FILING_CLOCK_STATES:
        return Filing(FilingStatus.NOT_APPLICABLE, None)
    if term is None or service_date is None:
        return Filing(FilingStatus.UNKNOWN, None)
    return Filing(FilingStatus.RUNNING, term - (today - service_date).days)


def claim_detail(
    session: Session, *, principal: Principal, claim_id: uuid.UUID, today: date
) -> ClaimDetail:
    claim = session.scalar(
        scoped_query(select(Claim).where(Claim.claim_id == claim_id), Claim, principal)
    )
    if claim is None:
        # Out of scope and never issued read the same (PD-20).
        raise ResourceNotFoundError(entity="claim", entity_id=str(claim_id))
    client = claim.client_id
    payer = session.get(Payer, claim.payer_id)

    lines = session.scalars(
        select(ClaimLine)
        .where(ClaimLine.client_id == client, ClaimLine.claim_id == claim.claim_id)
        .order_by(ClaimLine.line_number)
    ).all()
    # 837P usually dates the lines (DTP*472), not the claim: the clock starts
    # at the earliest service on it, as the queue counts it.
    line_dates = [line.service_date for line in lines if line.service_date]
    service_date = claim.date_of_service or (min(line_dates) if line_dates else None)

    # PD-7: one claim across its submissions. The payer answers each by its
    # own CLP01, so the remittance and the decisions are the whole chain's,
    # not only the submission that happens to be open.
    chain = session.scalars(
        select(Claim)
        .where(Claim.client_id == client, Claim.root_claim_id == claim.root_claim_id)
        .order_by(Claim.submission_attempt)
    ).all()
    payments = _payments(
        session,
        client=client,
        attempts={link.submitter_claim_id: link.submission_attempt for link in chain},
    )

    return ClaimDetail(
        claim_id=claim.claim_id,
        submitter_claim_id=claim.submitter_claim_id,
        state=claim.state,
        payer_id=claim.payer_id,
        payer_name=payer.name if payer else None,
        date_of_service=service_date,
        submitted_on=claim.submitted_on,
        next_action_due=claim.next_action_due,
        total_charge=claim.total_charge,
        balance=outstanding_balance(session, claim_id=claim.claim_id, client_id=client),
        member_id_masked=mask(claim.subscriber_member_id),
        place_of_service=claim.place_of_service,
        diagnoses=list(claim.diagnoses),
        filing=filing(
            ClaimState(claim.state),
            term=payer.timely_filing_days if payer else None,
            service_date=service_date,
            today=today,
        ),
        lines=[
            Line(
                line_number=line.line_number,
                procedure_code=line.procedure_code,
                modifiers=[
                    m
                    for m in (line.modifier_1, line.modifier_2, line.modifier_3, line.modifier_4)
                    if m
                ],
                units=line.units,
                charge=line.charge_amount,
                service_date=line.service_date,
                diagnosis_pointers=list(line.diagnosis_pointers),
            )
            for line in lines
        ],
        payments=payments,
        chain=[
            ChainLink(
                claim_id=link.claim_id,
                submitter_claim_id=link.submitter_claim_id,
                attempt=link.submission_attempt,
                state=link.state,
            )
            for link in chain
        ],
        timeline=[
            Event(
                occurred_at=row.occurred_at,
                source_state=row.source_state,
                target_state=row.target_state,
                reason=row.reason,
                note=row.note,
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                review_status=row.review_status,
            )
            for row in history(session, claim_id=claim.claim_id, client_id=client)
        ],
        decisions=_decisions(
            session,
            client=client,
            claim_ids=[link.claim_id for link in chain],
            payment_ids=[p.claim_payment_id for p in payments],
        ),
    )


def _adjustment(row: RemittanceAdjustment) -> Adjustment:
    return Adjustment(
        group_code=row.cas_group_code,
        reason_code=row.reason_code,
        amount=row.amount,
        claim_level=row.claim_level,
    )


def _payments(session: Session, *, client: uuid.UUID, attempts: dict[str, int]) -> list[Payment]:
    """Every 835 claim payment for the chain's submissions (CLP01), reversals
    included, each labelled with the submission it answers. Three queries
    however many payments: lines and adjustments are fetched together."""
    rows = session.execute(
        select(ClaimPayment, Remittance.payment_date)
        .join(Remittance, Remittance.remittance_id == ClaimPayment.remittance_id)
        .where(ClaimPayment.client_id == client, ClaimPayment.claim_id.in_(attempts))
        .order_by(ClaimPayment.created_at)
    ).all()
    ids = [payment.claim_payment_id for payment, _ in rows]
    lines: dict[uuid.UUID, list[RemittanceServiceLine]] = defaultdict(list)
    adjustments: dict[uuid.UUID, list[RemittanceAdjustment]] = defaultdict(list)
    if ids:
        for line in session.scalars(
            select(RemittanceServiceLine)
            .where(
                RemittanceServiceLine.client_id == client,
                RemittanceServiceLine.claim_payment_id.in_(ids),
            )
            .order_by(RemittanceServiceLine.line_number)
        ):
            lines[line.claim_payment_id].append(line)
        for adjustment in session.scalars(
            select(RemittanceAdjustment)
            .where(
                RemittanceAdjustment.client_id == client,
                RemittanceAdjustment.claim_payment_id.in_(ids),
            )
            .order_by(RemittanceAdjustment.sequence)
        ):
            adjustments[adjustment.claim_payment_id].append(adjustment)

    out: list[Payment] = []
    for payment, paid_on in rows:
        cas = adjustments[payment.claim_payment_id]
        out.append(
            Payment(
                claim_payment_id=payment.claim_payment_id,
                submitter_claim_id=payment.claim_id,
                attempt=attempts[payment.claim_id],
                payer_control_number=payment.payer_control_number,
                status_code=payment.status_code,
                payment_date=paid_on,
                total_charge=payment.total_charge,
                total_paid=payment.total_paid,
                patient_responsibility=payment.patient_responsibility,
                is_reversal=payment.is_reversal,
                lines=[
                    PaidLine(
                        line_number=line.line_number,
                        procedure_code=line.procedure_code,
                        modifiers=list(line.modifiers),
                        charge=line.charge_amount,
                        allowed=line.allowed_amount,
                        paid=line.paid_amount,
                        remark_codes=list(line.remark_codes),
                        # The schema keeps a claim-level adjustment off every
                        # line and a line's on its line, so none is dropped.
                        adjustments=[
                            _adjustment(a) for a in cas if a.service_line_id == line.service_line_id
                        ],
                    )
                    for line in lines[payment.claim_payment_id]
                ],
                claim_adjustments=[_adjustment(a) for a in cas if a.claim_level],
            )
        )
    return out


def _decisions(
    session: Session,
    *,
    client: uuid.UUID,
    claim_ids: list[uuid.UUID],
    payment_ids: list[uuid.UUID],
) -> list[Decision]:
    """Decisions about the chain's claims, their payments, those lines and
    their work.

    `decision_record` carries no client id; every id asked for here was read
    through the claim's own client, so the scope holds by construction.
    """
    line_ids: list[uuid.UUID] = []
    if payment_ids:
        line_ids = list(
            session.scalars(
                select(RemittanceServiceLine.service_line_id).where(
                    RemittanceServiceLine.client_id == client,
                    RemittanceServiceLine.claim_payment_id.in_(payment_ids),
                )
            ).all()
        )
    item_ids = list(
        session.scalars(
            select(WorkItem.work_item_id).where(
                WorkItem.client_id == client, WorkItem.claim_id.in_(claim_ids)
            )
        ).all()
    )
    wanted: list[tuple[str, list[uuid.UUID]]] = [
        ("claim", claim_ids),
        ("claim_payment", payment_ids),
        ("remittance_service_line", line_ids),
        ("work_item", item_ids),
    ]
    rows = session.scalars(
        select(DecisionRecord)
        .where(
            or_(
                *(
                    (DecisionRecord.entity_type == kind) & DecisionRecord.entity_id.in_(ids)
                    for kind, ids in wanted
                    if ids
                )
            )
        )
        .order_by(DecisionRecord.decided_at)
    ).all()
    return [
        Decision(
            decision_id=row.decision_id,
            decided_at=row.decided_at,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            decision_type=row.decision_type,
            decided_by=row.decided_by,
            logic_version=row.logic_version,
            inputs=dict(row.input_snapshot),
            output=dict(row.output),
            confidence=row.confidence,
        )
        for row in rows
    ]
