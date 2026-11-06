"""Denials classified and fed back, with what needs a person put before one
(M4.8, M4.9, ADR-0012, ADR-0013).

`rcm.decision` classifies denials and runs the feedback loop; it sits below
this layer and cannot open work. This module runs both and raises:

- each unmapped refusal as a named DATA_QUALITY exception, so a code nobody
  mapped is never just a number in a log line (PD-18);
- each rule that flagged a line the payer then denied for the same cause as
  a RULE_CONFLICT for the rules reviewer -- SRS 8.4's "DEFECT, rule failed";
- each preventable denial no submitted line could be traced to as a named
  DATA_QUALITY exception, since the loop could not learn from it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.decision import denials, feedback
from rcm.decision.denial_models import DenialClassification
from rcm.domain.claim import Claim
from rcm.domain.remittance import ClaimPayment
from rcm.platform.audit import DecisionType, record_decision
from rcm.platform.errors import ValidationFailedError
from rcm.platform.logging import get_logger
from rcm.work import exceptions
from rcm.work.denial_workbench import DENIAL_KEY
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import OPEN_STATES

log = get_logger(__name__)

#: One item per line per taxonomy version: a new version that still does not
#: hold the code raises it afresh.
_UNMAPPED_KEY: Final = "denial-unmapped:"
_FAILED_KEY: Final = "rule-failed:"
_UNTRACED_KEY: Final = "denial-untraced:"


def _source_key(line: denials.UnmappedLine) -> str:
    return f"{_UNMAPPED_KEY}{denials.TAXONOMY_VERSION}:{line.service_line_id}"


def _open(session: Session, *, client_id: uuid.UUID, source_key: str) -> bool:
    return (
        session.scalar(
            select(WorkItem.work_item_id)
            .where(
                WorkItem.client_id == client_id,
                WorkItem.kind == str(WorkItemKind.DATA_QUALITY),
                WorkItem.source_key == source_key,
                WorkItem.state.in_([str(s) for s in OPEN_STATES]),
            )
            .limit(1)
        )
        is not None
    )


@dataclass(frozen=True, slots=True)
class DenialRun:
    classification: denials.ClassificationReport
    feedback: feedback.FeedbackReport


def classify_and_route(session: Session, *, client_id: uuid.UUID, now: datetime) -> DenialRun:
    """Classify one client's denials, feed them back, and raise what needs a
    person.

    Idempotent: a second run classifies and answers nothing new, and finds
    each item it raised still open.
    """
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    report = denials.classify_denials(session, client_id=client_id)
    for line in report.unmapped:
        source_key = _source_key(line)
        if not _open(session, client_id=client_id, source_key=source_key):
            # Declining to classify is a decision like classifying (PD-1).
            # Recorded when the item is raised, not on every run that finds
            # it still open.
            record_decision(
                session,
                entity_type="remittance_service_line",
                entity_id=line.service_line_id,
                decision_type=DecisionType.CLASSIFY_DENIAL,
                decided_by=denials.DECIDED_BY,
                logic_version=denials.TAXONOMY_VERSION,
                input_snapshot={"refusal_carcs": list(line.carcs)},
                output={"held": "unmapped-refusal", "category": None},
            )
        exceptions.raise_exception(
            session,
            client_id=client_id,
            kind=WorkItemKind.DATA_QUALITY,
            # The codes first, so a cut title still names them.
            problem=(
                f"Refusal code(s) {', '.join(line.carcs)} on a denied 835 line are not in "
                f"denial taxonomy {denials.TAXONOMY_VERSION}; a person sets the category"
            ),
            record=exceptions.RecordRef("remittance_service_line", line.service_line_id),
            source_key=source_key,
            now=now,
        )
    answered = feedback.run_feedback(session, client_id=client_id)
    for failed in answered.failed:
        # Recorded by the loop's own decision record; raised once per line.
        exceptions.raise_exception(
            session,
            client_id=client_id,
            kind=WorkItemKind.RULE_CONFLICT,
            problem=(
                f"{failed.rule_id} flagged this claim before submission and the payer "
                "denied it for the same cause; the rule did not stop it"
            ),
            record=exceptions.RecordRef("claim", failed.claim_id),
            source_key=f"{_FAILED_KEY}{denials.TAXONOMY_VERSION}:{failed.service_line_id}",
            now=now,
            claim_id=failed.claim_id,
        )
    for untraced in answered.untraced:
        # A preventable denial no submitted line could be found for: the
        # loop could not learn from it, so a person traces it (PD-18).
        exceptions.raise_exception(
            session,
            client_id=client_id,
            kind=WorkItemKind.DATA_QUALITY,
            problem=untraced.reason,
            record=exceptions.RecordRef("claim_payment", untraced.claim_payment_id),
            source_key=f"{_UNTRACED_KEY}{denials.TAXONOMY_VERSION}:{untraced.service_line_id}",
            now=now,
        )
    for claim_payment_id in report.newly_classified:
        _raise_denial(session, client_id=client_id, claim_payment_id=claim_payment_id, now=now)
    session.flush()
    log.info(
        "denials.routed",
        unmapped=len(report.unmapped),
        rules_failed=len(answered.failed),
        untraced=len(answered.untraced),
        denials=len(report.newly_classified),
    )
    return DenialRun(classification=report, feedback=answered)


def _raise_denial(
    session: Session, *, client_id: uuid.UUID, claim_payment_id: uuid.UUID, now: datetime
) -> None:
    """S-04: put a newly denied claim in the Denial Workbench, once.

    Raised from the run that first classifies it (`newly_classified`), so a
    denial a person has worked does not come back. The title names the codes
    -- group and reason together (PD-3) -- never the patient.
    """
    source_key = f"{DENIAL_KEY}{claim_payment_id}"
    if session.scalar(
        select(WorkItem.work_item_id).where(
            WorkItem.client_id == client_id,
            WorkItem.kind == str(WorkItemKind.DENIAL),
            WorkItem.source_key == source_key,
        )
    ):
        # Ever raised -- open or finished. A payment classified afresh (a new
        # taxonomy version) must not bring back a denial a person worked.
        return
    payment = session.scalar(
        select(ClaimPayment).where(
            ClaimPayment.client_id == client_id,
            ClaimPayment.claim_payment_id == claim_payment_id,
        )
    )
    if payment is None:  # classified in this transaction; cannot vanish
        raise ValidationFailedError(field="claim_payment_id", reason="no such claim payment")
    codes = sorted(
        set(
            session.execute(
                select(DenialClassification.cas_group_code, DenialClassification.carc_code).where(
                    DenialClassification.client_id == client_id,
                    DenialClassification.claim_payment_id == claim_payment_id,
                    DenialClassification.taxonomy_version == denials.TAXONOMY_VERSION,
                )
            ).all()
        )
    )
    claim_uuid = session.scalar(
        select(Claim.claim_id).where(
            Claim.client_id == client_id, Claim.submitter_claim_id == payment.claim_id
        )
    )
    exceptions.raise_exception(
        session,
        client_id=client_id,
        kind=WorkItemKind.DENIAL,
        problem=(
            f"Payer denied {', '.join(f'{g}-{c}' for g, c in codes)}: decide whether to "
            "correct, appeal, bill the patient or write off"
        ),
        record=exceptions.RecordRef("claim_payment", claim_payment_id),
        source_key=source_key,
        now=now,
        # None when the 835 names a claim this client never submitted; the
        # item is still raised, and a person finds the claim (PD-18).
        claim_id=claim_uuid,
    )
