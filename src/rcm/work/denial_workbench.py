"""What S-04 Denial Workbench reads for one denial (M5.7 slice 1).

    PD-20: the work item is found through `scoped_query`; every other row is
    read by its client id.
    PD-3: each adjustment is group and reason together.
    BR-TAX-01: the category and preventability shown are the ones the
    classifier recorded, read back -- never re-derived here.
    UR-11: similar-denial history is on by default -- how often this payer
    denied this procedure for this code in the last year, and how those
    denials were resolved.

**Guidance is documentation, not a decision.** Each category carries a
short, versioned note on what usually resolves it (DOC-02 8.1). It is the
same for every claim in the category, so it decides nothing about this one;
the operator does. When a model starts recommending per claim (M6) that
will be a decision, with its own record (PD-1).
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.analytics.denials import is_refusal
from rcm.decision.denial_models import DenialClassification
from rcm.decision.denials import TAXONOMY_VERSION, DenialCategory
from rcm.domain.claim import Claim
from rcm.domain.payer import Payer
from rcm.domain.remittance import (
    ClaimPayment,
    Remittance,
    RemittanceAdjustment,
    RemittanceServiceLine,
)
from rcm.platform.auth import Principal, scoped_query
from rcm.platform.errors import ResourceNotFoundError
from rcm.work.claim_detail import Adjustment

if TYPE_CHECKING:
    from rcm.work.appeals import AppealView
from rcm.work.models import (
    WorkItem,
    WorkItemKind,
    WriteOff,
    WriteOffDecision,
    WriteOffDecisionKind,
)
from rcm.work.priority import outstanding_balance

GUIDANCE_VERSION: Final = "denial-guidance-v1"
SIMILAR_WINDOW_DAYS: Final = 365
DENIAL_KEY: Final = "denial:"

#: DOC-02 8.1, as a note to the operator. Plain words; no claim specifics.
GUIDANCE: Final[dict[DenialCategory, str]] = {
    DenialCategory.REGISTRATION_ELIGIBILITY: (
        "Coverage or patient details did not match. Check eligibility for the date of "
        "service; correct and resubmit, or bill other coverage or the patient."
    ),
    DenialCategory.AUTHORISATION: (
        "No valid authorisation on file. Find the authorisation and resubmit with it, or "
        "appeal with evidence it was obtained; a retro-authorisation may be possible."
    ),
    DenialCategory.CODING: (
        "The codes as billed were not accepted. Review the coding against the record; "
        "correct and resubmit."
    ),
    DenialCategory.MEDICAL_NECESSITY: (
        "The payer did not accept that the service was necessary. Appeal with clinical "
        "documentation if the record supports it."
    ),
    DenialCategory.TIMELY_FILING: (
        "Filed after the payer's deadline. Appeal only with proof of timely filing; "
        "otherwise this is usually written off."
    ),
    DenialCategory.DUPLICATE: (
        "The payer believes it has already processed this claim. Check for an earlier "
        "submission or payment before doing anything else."
    ),
    DenialCategory.COORDINATION_OF_BENEFITS: (
        "Another payer may be primary. Confirm coverage order and bill the right payer."
    ),
    DenialCategory.CONTRACTUAL_NON_COVERED: (
        "Not covered under the contract. This is usually the correct outcome; the CO "
        "amount is never billed to the patient."
    ),
    DenialCategory.PROVIDER_ENROLMENT: (
        "The provider is not enrolled or credentialed with this payer for this service. "
        "Fix enrolment, then resubmit."
    ),
    DenialCategory.PAYER_ERROR: (
        "The payer appears to have made an error. Appeal or request reprocessing."
    ),
}


@dataclass(frozen=True, slots=True)
class Classification:
    category: str
    preventability: str
    group_code: str
    reason_code: str
    taxonomy_version: str


@dataclass(frozen=True, slots=True)
class Guidance:
    category: str
    text: str
    version: str


@dataclass(frozen=True, slots=True)
class Similar:
    window_days: int
    #: Other claims' denials: same payer, procedure, group and reason.
    denials: int
    #: How the resolved ones ended, by structured outcome (FR-WORK-04).
    outcomes: dict[str, int]


class LineStatus(StrEnum):
    #: Denied, and classified under the current taxonomy.
    DENIED = "DENIED"
    #: Paid nothing for a refusal the taxonomy does not hold yet: a denial
    #: waiting for a person to set its category -- never shown as paid.
    UNCLASSIFIED = "UNCLASSIFIED"
    #: Not denied: paid, or paid nothing only for the patient's cost share.
    NOT_DENIED = "NOT_DENIED"


@dataclass(frozen=True, slots=True)
class DeniedLine:
    line_number: int
    status: LineStatus
    procedure_code: str
    modifiers: list[str]
    charge: Decimal
    paid: Decimal
    adjustments: list[Adjustment]
    #: None for a line the payer did not deny.
    classification: Classification | None
    guidance: Guidance | None
    similar: Similar | None


@dataclass(frozen=True, slots=True)
class ClaimSummary:
    claim_id: uuid.UUID
    submitter_claim_id: str
    state: str
    payer_id: str
    payer_name: str | None
    date_of_service: date | None
    balance: Decimal


@dataclass(frozen=True, slots=True)
class PaymentSummary:
    claim_payment_id: uuid.UUID
    payer_control_number: str
    payment_date: date | None
    total_charge: Decimal
    total_paid: Decimal
    patient_responsibility: Decimal


@dataclass(frozen=True, slots=True)
class WriteOffSummary:
    write_off_id: uuid.UUID
    #: APPLIED, PENDING_APPROVAL or REJECTED -- read from the insert-only
    #: request and its decision, never stored as a mutable flag.
    status: str
    amount: Decimal
    reason: str
    note: str
    requested_by: str
    requested_at: datetime
    decided_by: str | None


@dataclass(frozen=True, slots=True)
class DenialWorkbench:
    work_item_id: uuid.UUID
    state: str
    assigned_to: str | None
    sla_due_at: datetime
    outcome: str | None
    #: None when the 835 names a claim this client never submitted.
    claim: ClaimSummary | None
    payment: PaymentSummary
    lines: list[DeniedLine]
    #: CAS at claim level (PD-6): an adjustment like any other.
    claim_adjustments: list[Adjustment]
    #: Every write-off asked of this denial, newest first.
    write_offs: list[WriteOffSummary]
    #: The denial's appeal, if one was drafted (ADR-0017).
    appeal: AppealView | None


def _payment_id(item: WorkItem) -> uuid.UUID:
    key = item.source_key or ""
    if not key.startswith(DENIAL_KEY):
        raise ResourceNotFoundError(entity="denial", entity_id=str(item.work_item_id))
    return uuid.UUID(key.removeprefix(DENIAL_KEY))


def denial_workbench(
    session: Session, *, principal: Principal, work_item_id: uuid.UUID, now: datetime
) -> DenialWorkbench:
    item = session.scalar(
        scoped_query(
            select(WorkItem).where(
                WorkItem.work_item_id == work_item_id,
                WorkItem.kind == str(WorkItemKind.DENIAL),
            ),
            WorkItem,
            principal,
        )
    )
    if item is None:
        # Out of scope, never raised, or not a denial: all read the same (PD-20).
        raise ResourceNotFoundError(entity="denial", entity_id=str(work_item_id))
    client = item.client_id
    payment_id = _payment_id(item)
    row = session.execute(
        select(ClaimPayment, Remittance.payment_date, Remittance.payer_id)
        .join(Remittance, Remittance.remittance_id == ClaimPayment.remittance_id)
        .where(ClaimPayment.client_id == client, ClaimPayment.claim_payment_id == payment_id)
    ).first()
    if row is None:
        raise ResourceNotFoundError(entity="denial", entity_id=str(work_item_id))
    payment, paid_on, payer_id = row

    lines = session.scalars(
        select(RemittanceServiceLine)
        .where(
            RemittanceServiceLine.client_id == client,
            RemittanceServiceLine.claim_payment_id == payment_id,
        )
        .order_by(RemittanceServiceLine.line_number)
    ).all()
    adjustments: dict[uuid.UUID | None, list[RemittanceAdjustment]] = defaultdict(list)
    for adjustment in session.scalars(
        select(RemittanceAdjustment)
        .where(
            RemittanceAdjustment.client_id == client,
            RemittanceAdjustment.claim_payment_id == payment_id,
        )
        .order_by(RemittanceAdjustment.sequence)
    ):
        adjustments[adjustment.service_line_id].append(adjustment)
    classified = {
        c.service_line_id: c
        for c in session.scalars(
            select(DenialClassification).where(
                DenialClassification.client_id == client,
                DenialClassification.claim_payment_id == payment_id,
                # BR-TAX-01: one category per denial -- the current table's.
                DenialClassification.taxonomy_version == TAXONOMY_VERSION,
            )
        )
    }

    # UR-11's window: the year before this denial, by the payer's date --
    # not by when the classifier ran (a back-load would count everything).
    anchor = paid_on or now.date()
    chain = _chain_submitter_ids(session, client=client, submitter_claim_id=payment.claim_id)
    out: list[DeniedLine] = []
    for line in lines:
        found = classified.get(line.service_line_id)
        out.append(
            DeniedLine(
                line_number=line.line_number,
                status=_status(found, line, refused=_refused(adjustments[line.service_line_id])),
                procedure_code=line.procedure_code,
                modifiers=list(line.modifiers),
                charge=line.charge_amount,
                paid=line.paid_amount,
                adjustments=[
                    Adjustment(
                        group_code=a.cas_group_code,
                        reason_code=a.reason_code,
                        amount=a.amount,
                        claim_level=a.claim_level,
                    )
                    for a in adjustments[line.service_line_id]
                ],
                classification=_classification(found),
                guidance=_guidance(found),
                similar=(
                    None
                    if found is None
                    else _similar(
                        session,
                        client=client,
                        payer_id=payer_id,
                        procedure_code=line.procedure_code,
                        found=found,
                        window=(anchor - timedelta(days=SIMILAR_WINDOW_DAYS), anchor),
                        chain=chain,
                    )
                ),
            )
        )

    return DenialWorkbench(
        work_item_id=item.work_item_id,
        state=item.state,
        assigned_to=item.assigned_to,
        sla_due_at=item.sla_due_at,
        outcome=item.outcome,
        claim=_claim(session, item=item),
        payment=PaymentSummary(
            claim_payment_id=payment.claim_payment_id,
            payer_control_number=payment.payer_control_number,
            payment_date=paid_on,
            total_charge=payment.total_charge,
            total_paid=payment.total_paid,
            patient_responsibility=payment.patient_responsibility,
        ),
        lines=out,
        claim_adjustments=[
            Adjustment(
                group_code=a.cas_group_code,
                reason_code=a.reason_code,
                amount=a.amount,
                claim_level=a.claim_level,
            )
            for a in adjustments[None]
        ],
        write_offs=_write_offs(session, item=item),
        appeal=_appeal(session, item=item),
    )


def _appeal(session: Session, *, item: WorkItem) -> AppealView | None:
    # Imported here: rcm.work.appeals reads this module to draft a letter.
    from rcm.work.appeals import current_appeal

    return current_appeal(session, item=item)


def _write_offs(session: Session, *, item: WorkItem) -> list[WriteOffSummary]:
    rows = session.execute(
        select(WriteOff, WriteOffDecision)
        .outerjoin(WriteOffDecision, WriteOffDecision.write_off_id == WriteOff.write_off_id)
        .where(WriteOff.client_id == item.client_id, WriteOff.work_item_id == item.work_item_id)
        .order_by(WriteOff.requested_at.desc())
    ).all()
    out: list[WriteOffSummary] = []
    for write_off, joined in rows:
        # An outer join: no decision yet reads as None, whatever the row type says.
        decision: WriteOffDecision | None = joined
        if decision is not None:
            status = (
                "APPLIED" if decision.decision == str(WriteOffDecisionKind.APPROVED) else "REJECTED"
            )
        else:
            status = "PENDING_APPROVAL" if write_off.needs_approval else "APPLIED"
        out.append(
            WriteOffSummary(
                write_off_id=write_off.write_off_id,
                status=status,
                amount=write_off.amount,
                reason=write_off.reason,
                note=write_off.note,
                requested_by=write_off.requested_by,
                requested_at=write_off.requested_at,
                decided_by=decision.decided_by if decision is not None else None,
            )
        )
    return out


def _refused(adjustments: list[RemittanceAdjustment]) -> bool:
    return any(is_refusal(a.cas_group_code, a.reason_code) for a in adjustments)


def _status(
    found: DenialClassification | None, line: RemittanceServiceLine, *, refused: bool
) -> LineStatus:
    if found is not None:
        return LineStatus.DENIED
    if line.paid_amount == 0 and refused:
        return LineStatus.UNCLASSIFIED
    return LineStatus.NOT_DENIED


def _chain_submitter_ids(
    session: Session, *, client: uuid.UUID, submitter_claim_id: str
) -> list[str]:
    """Every CLP01 of the claim this payment answers (PD-7): the original and
    its resubmissions are one claim, so none is another's similar denial."""
    root = session.scalar(
        select(Claim.root_claim_id).where(
            Claim.client_id == client, Claim.submitter_claim_id == submitter_claim_id
        )
    )
    if root is None:
        return [submitter_claim_id]
    return list(
        session.scalars(
            select(Claim.submitter_claim_id).where(
                Claim.client_id == client, Claim.root_claim_id == root
            )
        ).all()
    )


def _classification(found: DenialClassification | None) -> Classification | None:
    if found is None:
        return None
    return Classification(
        category=found.category,
        preventability=found.preventability,
        group_code=found.cas_group_code,
        reason_code=found.carc_code,
        taxonomy_version=found.taxonomy_version,
    )


def _guidance(found: DenialClassification | None) -> Guidance | None:
    if found is None:
        return None
    text = GUIDANCE.get(DenialCategory(found.category))
    if text is None:  # a category the table does not cover says so (PD-18)
        text = "No guidance is written for this category yet."
    return Guidance(category=found.category, text=text, version=GUIDANCE_VERSION)


def _claim(session: Session, *, item: WorkItem) -> ClaimSummary | None:
    if item.claim_id is None:
        return None
    claim = session.scalar(
        select(Claim).where(Claim.client_id == item.client_id, Claim.claim_id == item.claim_id)
    )
    if claim is None:
        return None
    payer = session.get(Payer, claim.payer_id)
    return ClaimSummary(
        claim_id=claim.claim_id,
        submitter_claim_id=claim.submitter_claim_id,
        state=claim.state,
        payer_id=claim.payer_id,
        payer_name=payer.name if payer else None,
        date_of_service=claim.date_of_service,
        balance=outstanding_balance(session, claim_id=claim.claim_id, client_id=claim.client_id),
    )


def _similar(
    session: Session,
    *,
    client: uuid.UUID,
    payer_id: str,
    procedure_code: str,
    found: DenialClassification,
    window: tuple[date, date],
    chain: list[str],
) -> Similar:
    """Other claims' payments with the same payer, procedure, group and reason,
    paid in the window -- and the outcomes their denial items ended in."""
    payments = list(
        session.scalars(
            select(DenialClassification.claim_payment_id)
            .distinct()
            .join(
                RemittanceServiceLine,
                RemittanceServiceLine.service_line_id == DenialClassification.service_line_id,
            )
            .join(
                ClaimPayment,
                ClaimPayment.claim_payment_id == DenialClassification.claim_payment_id,
            )
            .join(Remittance, Remittance.remittance_id == ClaimPayment.remittance_id)
            .where(
                DenialClassification.client_id == client,
                DenialClassification.cas_group_code == found.cas_group_code,
                DenialClassification.carc_code == found.carc_code,
                DenialClassification.taxonomy_version == TAXONOMY_VERSION,
                Remittance.payment_date >= window[0],
                Remittance.payment_date <= window[1],
                ClaimPayment.claim_id.not_in(chain),
                RemittanceServiceLine.procedure_code == procedure_code,
                Remittance.payer_id == payer_id,
            )
        ).all()
    )
    outcomes: Counter[str] = Counter()
    if payments:
        for outcome, count in session.execute(
            select(WorkItem.outcome, func.count())
            .where(
                WorkItem.client_id == client,
                WorkItem.kind == str(WorkItemKind.DENIAL),
                WorkItem.source_key.in_([f"{DENIAL_KEY}{p}" for p in payments]),
                WorkItem.outcome.is_not(None),
            )
            .group_by(WorkItem.outcome)
        ):
            if outcome is not None:  # filtered above; the guard is for the type checker
                outcomes[outcome] = count
    return Similar(window_days=SIMILAR_WINDOW_DAYS, denials=len(payments), outcomes=dict(outcomes))
