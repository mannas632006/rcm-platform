"""What a payer's answer to a status enquiry puts in front of a person
(DOC-02 M10, EC-54, EC-56; ADR-0033).

DOC-02's AR follow-up flow, for the answers a 277 (005010X212) can give:

    Finalised, payment pending  -> wait, reschedule check
    Paid - 835 not received     -> chase remittance            (EC-54)
    Denied                      -> route to M11
    Not on file                 -> resubmit, likely never received
    Pended for info             -> human queue with reason
    No response                 -> portal, then a human call

**An answer never moves a claim.** The claim's outcome is the 835's to give
(PD-4): a payer saying "paid" or "denied" here says the 835 should exist, and
when it does not, someone asks for it. Every item below names the claim and
the payer's own codes, never the payer's free text, which can carry a
patient's details (PD-13).

**Only a claim the payer has, and has not answered on an 835**, is read: one
Submitted or InAdjudication with no live 835 posting. A claim under appeal
is the appeal's, and is not worked again here (EC-57).

Idempotent: an item is raised once while it is open, and each raise is a
recorded decision (PD-1).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from rcm.domain.acknowledgement import ClaimStatusAnswer
from rcm.domain.claim import Claim
from rcm.domain.claim_state import ClaimState
from rcm.domain.remittance import ClaimPayment
from rcm.ingestion.x12.claim_status import StatusAnswer
from rcm.platform.audit import DecisionType, record_decision
from rcm.platform.config import get_settings
from rcm.platform.errors import ValidationFailedError
from rcm.platform.logging import get_logger
from rcm.work import exceptions
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import OPEN_STATES

log = get_logger(__name__)

LOGIC_VERSION: Final = "status-follow-up-v1"
ACTOR: Final = "system:status_follow_up"

#: Where the payer has the claim and has not answered on an 835.
_WAITING: Final = frozenset({ClaimState.SUBMITTED, ClaimState.IN_ADJUDICATION})

#: STC01-2 codes that say nothing about why a claim is pending: 0, "cannot
#: provide further status electronically", and 20, "accepted for
#: processing". A pend carrying only these, and no STC12 text, states no
#: reason (EC-56).
_NO_REASON_CODES: Final = frozenset({"", "0", "20"})

#: The answers that say the claim is decided, so an 835 should follow.
_DECIDED: Final = frozenset(
    {StatusAnswer.PAID, StatusAnswer.DENIED, StatusAnswer.NO_PAYMENT, StatusAnswer.FINALIZED}
)

_DECIDED_TEXT: Final[dict[StatusAnswer, str]] = {
    StatusAnswer.PAID: "says the claim was paid",
    StatusAnswer.DENIED: "says the claim was denied",
    StatusAnswer.NO_PAYMENT: "says adjudication is complete with no payment",
    StatusAnswer.FINALIZED: "says the claim is finalised",
}


@dataclass(frozen=True, slots=True)
class FollowUp:
    """One thing a person must do about one claim."""

    key: str
    problem: str
    detail: str


@dataclass(slots=True)
class StatusFollowUpReport:
    claims_read: int = 0
    raised: int = 0
    #: Answers that need nothing yet: acknowledged, a pend that states its
    #: reason, a decided claim still inside the wait for its 835.
    waiting: int = 0
    by_key: dict[str, int] = field(default_factory=dict)


def _codes(answer: ClaimStatusAnswer) -> str:
    if answer.status_code:
        return f"{answer.category_code}:{answer.status_code}"
    return answer.category_code


def _states_a_reason(answer: ClaimStatusAnswer) -> bool:
    return bool(answer.description.strip()) or answer.status_code.strip() not in _NO_REASON_CODES


def decide(answers: Sequence[ClaimStatusAnswer], *, now: datetime) -> FollowUp | None:
    """The follow-up the latest answers call for, or None to wait.

    `answers` are one claim's, oldest first. No database; the only clock is
    the one passed in.
    """
    if not answers:
        return None
    settings = get_settings()
    latest = answers[-1]
    kind = StatusAnswer(latest.answer)
    codes = _codes(latest)

    if kind in _DECIDED:
        waited = (now.date() - latest.received_at.date()).days
        if waited < settings.status_remittance_grace_days:
            return None
        return FollowUp(
            key="status-decided-no-835",
            problem=(
                f"The payer's status answer ({codes}) {_DECIDED_TEXT[kind]}, but no 835 is on "
                "file: ask the payer for the remittance"
            ),
            detail=(
                "EC-54. The 835 decides the claim's outcome; until it arrives the claim waits "
                "in adjudication. If the payer says funds were sent, check the deposit."
            ),
        )

    if kind is StatusAnswer.PENDING:
        run: list[ClaimStatusAnswer] = []
        for answer in reversed(answers):
            if StatusAnswer(answer.answer) is not StatusAnswer.PENDING:
                break
            run.append(answer)
        if len(run) < settings.status_pending_repeat_limit or any(_states_a_reason(a) for a in run):
            return None
        return FollowUp(
            key="status-pending-no-reason",
            problem=(
                f"The payer has pended this claim {len(run)} times running without stating "
                "a reason: call the payer and ask what it is waiting for"
            ),
            detail="EC-56. A pend that states a reason is waited out; this one never has.",
        )

    if kind is StatusAnswer.INFO_REQUESTED:
        return FollowUp(
            key="status-info-requested",
            problem=(
                f"The payer asks for information before it decides ({codes}): send what it asks for"
            ),
            detail="The payer's own words are on the status answer, with its codes.",
        )

    if kind is StatusAnswer.NOT_FOUND:
        return FollowUp(
            key="status-not-found",
            problem=(
                f"The payer has no record of this claim ({codes}): confirm it was received, "
                "and resend it if it was not"
            ),
            detail="DOC-02 M10: not on file usually means never received.",
        )

    if kind is StatusAnswer.RETURNED:
        return FollowUp(
            key="status-returned",
            problem=f"The payer returned this claim unprocessed ({codes}): correct it and resend",
            detail="A returned claim is a rejection, not a denial (PD-4).",
        )

    if kind is StatusAnswer.NO_ANSWER:
        return FollowUp(
            key="status-no-answer",
            problem=(
                f"The payer could not answer the status enquiry ({codes}): check the portal or call"
            ),
            detail="DOC-02 M10: no electronic answer goes to the portal, then a call.",
        )

    return None  # ACKNOWLEDGED: the payer has it; nothing to do yet.


def _posted(session: Session, claim: Claim) -> bool:
    """A live 835 posting is on file: the 835 has answered the claim."""
    reversal = aliased(ClaimPayment)
    return (
        session.scalar(
            select(ClaimPayment.claim_payment_id)
            .where(
                ClaimPayment.client_id == claim.client_id,
                ClaimPayment.claim_id == claim.submitter_claim_id,
                ClaimPayment.is_reversal.is_(False),
                ~select(reversal.claim_payment_id)
                .where(
                    reversal.client_id == claim.client_id,
                    reversal.reverses_claim_payment_id == ClaimPayment.claim_payment_id,
                )
                .exists(),
            )
            .limit(1)
        )
        is not None
    )


def _open(session: Session, claim: Claim, key: str) -> bool:
    return (
        session.scalar(
            select(WorkItem.work_item_id)
            .where(
                WorkItem.client_id == claim.client_id,
                WorkItem.kind == str(WorkItemKind.CLAIM_STATUS_FOLLOW_UP),
                WorkItem.source_key == f"{key}:{claim.claim_id}",
                WorkItem.state.in_([str(s) for s in OPEN_STATES]),
            )
            .limit(1)
        )
        is not None
    )


def follow_up_client(
    session: Session, *, client_id: uuid.UUID, now: datetime
) -> StatusFollowUpReport:
    """Read one client's status answers and raise what needs a person."""
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    report = StatusFollowUpReport()
    answered = session.scalars(
        select(ClaimStatusAnswer)
        .where(ClaimStatusAnswer.client_id == client_id)
        .order_by(
            ClaimStatusAnswer.claim_id,
            ClaimStatusAnswer.received_at,
            ClaimStatusAnswer.created_at,
            ClaimStatusAnswer.answer_id,
        )
    ).all()
    by_claim: dict[str, list[ClaimStatusAnswer]] = {}
    for answer in answered:
        by_claim.setdefault(answer.claim_id, []).append(answer)
    if not by_claim:
        return report

    claims = session.scalars(
        select(Claim).where(
            Claim.client_id == client_id,
            Claim.submitter_claim_id.in_(list(by_claim)),
            Claim.state.in_([str(s) for s in _WAITING]),
        )
    ).all()
    for claim in claims:
        report.claims_read += 1
        if _posted(session, claim):
            continue
        answers = by_claim[claim.submitter_claim_id]
        follow_up = decide(answers, now=now)
        if follow_up is None:
            report.waiting += 1
            continue
        if not _open(session, claim, follow_up.key):
            latest = answers[-1]
            record_decision(
                session,
                entity_type="claim",
                entity_id=claim.claim_id,
                decision_type=DecisionType.STATUS_FOLLOW_UP,
                decided_by=ACTOR,
                logic_version=LOGIC_VERSION,
                input_snapshot={
                    "state": claim.state,
                    "answers": [
                        {
                            "answer": a.answer,
                            "category_code": a.category_code,
                            "status_code": a.status_code,
                            "states_a_reason": _states_a_reason(a),
                        }
                        for a in answers
                    ],
                    "latest_received_on": latest.received_at.date().isoformat(),
                },
                output={"follow_up": follow_up.key},
            )
            report.raised += 1
            report.by_key[follow_up.key] = report.by_key.get(follow_up.key, 0) + 1
        exceptions.raise_exception(
            session,
            client_id=claim.client_id,
            kind=WorkItemKind.CLAIM_STATUS_FOLLOW_UP,
            problem=follow_up.problem,
            record=exceptions.RecordRef("claim", claim.claim_id),
            source_key=f"{follow_up.key}:{claim.claim_id}",
            now=now,
            claim_id=claim.claim_id,
            detail=follow_up.detail,
        )
    session.flush()
    log.info(
        "status_follow_up.done",
        claims_read=report.claims_read,
        raised=report.raised,
        waiting=report.waiting,
    )
    return report
