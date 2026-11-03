"""Denial taxonomy and classification (M4.8, DOC-02 8.1, ADR-0012).

BR-M11-01: every denial is classified by root cause, not merely recorded by
CARC. BR-M11-02: every denial gets a preventability determination.
BR-TAX-01: exactly one category and one preventability per denial, from a
version-controlled CARC -> category table.

What a denial *is* -- a zero-paid line with a reason that is neither the
patient's cost share nor a payment adjustment -- is `rcm.analytics.denials`'s,
so the denial rate, the warehouse and this module agree. This module says
which kind it is.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from rcm.analytics.denials import is_refusal
from rcm.decision.denial_models import DenialClassification
from rcm.domain.remittance import ClaimPayment, RemittanceAdjustment, RemittanceServiceLine
from rcm.platform.audit import DecisionType, record_decision
from rcm.platform.logging import get_logger

log = get_logger(__name__)

#: Bump on any change to the table below; recorded with every classification.
TAXONOMY_VERSION: Final = "taxonomy-v1"
DECIDED_BY: Final = "rules_engine:denial_classifier"


class DenialCategory(StrEnum):
    """DOC-02 8.1, in its order."""

    REGISTRATION_ELIGIBILITY = "REGISTRATION_ELIGIBILITY"
    AUTHORISATION = "AUTHORISATION"
    CODING = "CODING"
    MEDICAL_NECESSITY = "MEDICAL_NECESSITY"
    TIMELY_FILING = "TIMELY_FILING"
    DUPLICATE = "DUPLICATE"
    COORDINATION_OF_BENEFITS = "COORDINATION_OF_BENEFITS"
    CONTRACTUAL_NON_COVERED = "CONTRACTUAL_NON_COVERED"
    PROVIDER_ENROLMENT = "PROVIDER_ENROLMENT"
    PAYER_ERROR = "PAYER_ERROR"


class Preventability(StrEnum):
    PREVENTABLE = "PREVENTABLE"
    PARTIALLY_PREVENTABLE = "PARTIALLY_PREVENTABLE"
    NOT_PREVENTABLE = "NOT_PREVENTABLE"


@dataclass(frozen=True, slots=True)
class CategoryRule:
    preventability: Preventability
    #: DOC-02's module that could have prevented it, or why none could.
    preventable_at: str


C = DenialCategory
P = Preventability

#: DOC-02 8.1, as drawn.
CATEGORY_RULES: Final[dict[DenialCategory, CategoryRule]] = {
    C.REGISTRATION_ELIGIBILITY: CategoryRule(P.PREVENTABLE, "M2/M3"),
    C.AUTHORISATION: CategoryRule(P.PREVENTABLE, "M4"),
    C.CODING: CategoryRule(P.PREVENTABLE, "M5/M7"),
    C.MEDICAL_NECESSITY: CategoryRule(P.PARTIALLY_PREVENTABLE, "LCD checking"),
    C.TIMELY_FILING: CategoryRule(P.PREVENTABLE, "M8/M10"),
    C.DUPLICATE: CategoryRule(P.PREVENTABLE, "M7/M8"),
    C.COORDINATION_OF_BENEFITS: CategoryRule(P.PREVENTABLE, "M2/M3"),
    C.CONTRACTUAL_NON_COVERED: CategoryRule(P.NOT_PREVENTABLE, "correct outcome"),
    C.PROVIDER_ENROLMENT: CategoryRule(P.PREVENTABLE, "M1"),
    C.PAYER_ERROR: CategoryRule(P.NOT_PREVENTABLE, "appeal"),
}

#: taxonomy-v1. Mirrored row for row by core_denial_taxonomy.sql; a test
#: fails if they drift. PAYER_ERROR has no CARC: a person assigns it.
CARC_CATEGORY: Final[dict[str, DenialCategory]] = {
    "4": C.CODING,  # procedure inconsistent with modifier
    "11": C.CODING,  # diagnosis inconsistent with procedure
    "15": C.AUTHORISATION,  # authorization number missing or invalid
    "16": C.REGISTRATION_ELIGIBILITY,  # lacks information (EC-59: often no RARC)
    "18": C.DUPLICATE,
    "22": C.COORDINATION_OF_BENEFITS,
    "26": C.REGISTRATION_ELIGIBILITY,  # before coverage
    "27": C.REGISTRATION_ELIGIBILITY,  # after coverage terminated
    "29": C.TIMELY_FILING,
    "31": C.REGISTRATION_ELIGIBILITY,  # patient not identified as insured
    "49": C.CONTRACTUAL_NON_COVERED,  # routine exam not covered
    "50": C.MEDICAL_NECESSITY,
    "54": C.CODING,  # multiple physicians / assistants
    "55": C.MEDICAL_NECESSITY,  # experimental
    "58": C.CODING,  # place of service
    "59": C.CODING,  # multiple or concurrent procedure rules
    "96": C.CONTRACTUAL_NON_COVERED,
    "97": C.CODING,  # bundled (NCCI)
    "109": C.REGISTRATION_ELIGIBILITY,  # not covered by this payer
    "119": C.CONTRACTUAL_NON_COVERED,  # benefit maximum
    "140": C.REGISTRATION_ELIGIBILITY,  # ID and name do not match
    "146": C.CODING,  # diagnosis invalid for date
    "147": C.PROVIDER_ENROLMENT,  # contracted rate not on file
    "151": C.MEDICAL_NECESSITY,  # units not supported
    "167": C.MEDICAL_NECESSITY,  # diagnosis not covered
    "170": C.PROVIDER_ENROLMENT,  # provider type
    "181": C.CODING,  # procedure code invalid on date
    "182": C.CODING,  # modifier invalid on date
    "183": C.PROVIDER_ENROLMENT,  # referring provider not eligible
    "197": C.AUTHORISATION,  # precertification absent
    "198": C.AUTHORISATION,  # precertification exceeded
    "204": C.CONTRACTUAL_NON_COVERED,  # not covered under plan
    "252": C.MEDICAL_NECESSITY,  # documentation required to support it
}


@dataclass(frozen=True, slots=True)
class Reason:
    """One CAS adjustment on a line."""

    group: str
    carc: str
    amount: Decimal
    sequence: int
    #: The last tie-break. CAS sequence is not unique on a line, so without
    #: it two equal reasons would be ordered by chance -- here and in the mart.
    adjustment_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Classification:
    category: DenialCategory
    preventability: Preventability
    #: The reason that decided it, and every reason on the line.
    group: str
    carc: str
    considered: tuple[Reason, ...]


@dataclass(frozen=True, slots=True)
class Unmapped:
    """A refusal the taxonomy does not hold. Reported, never guessed (PD-18)."""

    carcs: tuple[str, ...]


def classify_line(reasons: Sequence[Reason]) -> Classification | Unmapped | None:
    """One category for a denied line's reasons; None when none is a refusal.

    EC-62: several refusals on one line -- the largest decides, a tie goes to
    the earliest reason, then the adjustment id. Matches the mart's
    `line_denial` exactly. When the deciding refusal is not in the table the
    line is Unmapped even if a smaller refusal is: the category comes from the
    reason that cost the most, or from a person, never from a runner-up.
    """
    refusals = [r for r in reasons if is_refusal(r.group, r.carc)]
    if not refusals:
        return None
    deciding = min(refusals, key=lambda r: (-r.amount, r.sequence, r.adjustment_id))
    category = CARC_CATEGORY.get(deciding.carc)
    if category is None:
        return Unmapped(carcs=tuple(sorted({r.carc for r in refusals})))
    return Classification(
        category=category,
        preventability=CATEGORY_RULES[category].preventability,
        group=deciding.group,
        carc=deciding.carc,
        considered=tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class UnmappedLine:
    """A denied line left unclassified, for the work layer to put in front of
    a person. CARCs are codes, not PHI."""

    service_line_id: uuid.UUID
    claim_payment_id: uuid.UUID
    line_number: int
    carcs: tuple[str, ...]


@dataclass(slots=True)
class ClassificationReport:
    """Every zero-paid line read lands in exactly one count."""

    classified: int = 0
    #: Classified under this taxonomy version by a concurrent run between
    #: this run's read and its insert: nothing changed.
    already: int = 0
    #: Paid nothing, but no reason was a refusal (cost share, adjustment).
    not_refused: int = 0
    #: Paid nothing and gave no reason at all: not an adjudicated denial.
    no_reason: int = 0
    #: Denied lines whose refusal the taxonomy does not hold.
    unmapped: list[UnmappedLine] = field(default_factory=list)


#: Rows per INSERT. Eight binds a row keeps a batch far under Postgres's
#: 65,535-parameter ceiling.
_BATCH: Final = 1000


def classify_denials(session: Session, *, client_id: uuid.UUID) -> ClassificationReport:
    """Classify every denied line of one client's 835 postings.

    A posting the payer later reversed is read like any other: the denial
    happened, and its root cause is still a lesson (ADR-0012). The reversal
    posting itself, all signs flipped, is not.

    Idempotent per taxonomy version: a line already classified is not read
    again, and one a concurrent run classifies first is counted in
    `already`, not inserted twice. A line whose refusal the taxonomy does
    not hold is returned in `unmapped` and left unclassified (PD-18);
    raising it with a person is the work layer's (`rcm.work.denials`).
    """
    report = ClassificationReport()
    done = aliased(DenialClassification)
    # Zero-paid lines of original postings not yet classified under this
    # version, with their reasons. One statement and no bind per line, so a
    # client's size never meets the parameter ceiling.
    rows = session.execute(
        select(
            RemittanceServiceLine.service_line_id,
            RemittanceServiceLine.claim_payment_id,
            RemittanceServiceLine.line_number,
            RemittanceAdjustment.adjustment_id,
            RemittanceAdjustment.cas_group_code,
            RemittanceAdjustment.reason_code,
            RemittanceAdjustment.amount,
            RemittanceAdjustment.sequence,
        )
        .join(
            ClaimPayment,
            ClaimPayment.claim_payment_id == RemittanceServiceLine.claim_payment_id,
        )
        .outerjoin(
            RemittanceAdjustment,
            and_(
                RemittanceAdjustment.service_line_id == RemittanceServiceLine.service_line_id,
                RemittanceAdjustment.client_id == client_id,
            ),
        )
        .where(
            RemittanceServiceLine.client_id == client_id,
            ClaimPayment.client_id == client_id,
            RemittanceServiceLine.paid_amount == 0,
            # A reversal takes a payment back with every sign flipped; it is
            # not fresh evidence of a refusal (BR-M9-04), as in the rate.
            ClaimPayment.is_reversal.is_(False),
            ~select(done.service_line_id)
            .where(
                done.client_id == client_id,
                done.taxonomy_version == TAXONOMY_VERSION,
                done.service_line_id == RemittanceServiceLine.service_line_id,
            )
            .exists(),
        )
        .order_by(
            RemittanceServiceLine.claim_payment_id,
            RemittanceServiceLine.line_number,
            RemittanceAdjustment.sequence,
            RemittanceAdjustment.adjustment_id,
        )
    ).all()

    lines: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
    reasons: dict[uuid.UUID, list[Reason]] = defaultdict(list)
    for row in rows:
        lines[row.service_line_id] = (row.claim_payment_id, row.line_number)
        if row.adjustment_id is not None:
            reasons[row.service_line_id].append(
                Reason(
                    group=row.cas_group_code,
                    carc=row.reason_code,
                    amount=row.amount,
                    sequence=row.sequence,
                    adjustment_id=row.adjustment_id,
                )
            )

    pending: list[dict[str, object]] = []
    results: dict[uuid.UUID, Classification] = {}
    for service_line_id, (claim_payment_id, line_number) in lines.items():
        if not reasons[service_line_id]:
            report.no_reason += 1
            continue
        result = classify_line(reasons[service_line_id])
        if result is None:
            report.not_refused += 1
            continue
        if isinstance(result, Unmapped):
            report.unmapped.append(
                UnmappedLine(
                    service_line_id=service_line_id,
                    claim_payment_id=claim_payment_id,
                    line_number=line_number,
                    carcs=result.carcs,
                )
            )
            continue
        results[service_line_id] = result
        pending.append(
            {
                "service_line_id": service_line_id,
                "client_id": client_id,
                "claim_payment_id": claim_payment_id,
                "taxonomy_version": TAXONOMY_VERSION,
                "category": str(result.category),
                "preventability": str(result.preventability),
                "cas_group_code": result.group,
                "carc_code": result.carc,
            }
        )

    inserted: set[uuid.UUID] = set()
    for start in range(0, len(pending), _BATCH):
        inserted.update(
            session.scalars(
                pg_insert(DenialClassification)
                .values(pending[start : start + _BATCH])
                .on_conflict_do_nothing()
                .returning(DenialClassification.service_line_id)
            )
        )
    report.classified = len(inserted)
    report.already = len(results) - len(inserted)

    by_payment: dict[uuid.UUID, list[dict[str, object]]] = defaultdict(list)
    for service_line_id in sorted(inserted, key=lambda i: lines[i]):
        result = results[service_line_id]
        by_payment[lines[service_line_id][0]].append(
            {
                "line_number": lines[service_line_id][1],
                "category": str(result.category),
                "preventability": str(result.preventability),
                "deciding_reason": f"{result.group}-{result.carc}",
                "reasons": [f"{r.group}-{r.carc}" for r in result.considered],
            }
        )
    unmapped_by_payment: dict[uuid.UUID, list[dict[str, object]]] = defaultdict(list)
    for line in report.unmapped:
        unmapped_by_payment[line.claim_payment_id].append(
            {"line_number": line.line_number, "carcs": list(line.carcs)}
        )

    for claim_payment_id, classified in by_payment.items():
        record_decision(
            session,
            entity_type="claim_payment",
            entity_id=claim_payment_id,
            decision_type=DecisionType.CLASSIFY_DENIAL,
            decided_by=DECIDED_BY,
            logic_version=TAXONOMY_VERSION,
            input_snapshot={"lines": [c["reasons"] for c in classified]},
            # A payment can hold classified and unmapped lines at once; the
            # record says so rather than look complete.
            output={
                "lines": classified,
                "unmapped": unmapped_by_payment.get(claim_payment_id, []),
            },
        )
    session.flush()
    log.info(
        "denials.classified",
        classified=report.classified,
        already=report.already,
        not_refused=report.not_refused,
        no_reason=report.no_reason,
        unmapped=len(report.unmapped),
    )
    return report
