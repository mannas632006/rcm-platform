"""Claims move through the state machine on their own; the submitter approves.

Owner decision (2026-11-01): "everything done automatically but visible to
the user who submitted the claim, so that he can review it and approve the
system's steps." ADR-0006.

**What moves a claim.** Only evidence already on file:

- an 837 we hold was sent, so every step before `Submitted` happened;
- a 277CA verdict accepts (`InAdjudication`) or rejects (`Rejected`, PD-4);
- an 835 adjudicated it, so it was accepted even without a 277CA -- and
  the history says the acceptance was inferred rather than pretending a
  277CA arrived;
- a later submission chained to it by REF*F8 replaces it.

A 999 moves nothing (BR-M8-02: syntax, not acceptance). Every step after an
outcome -- appeal, write-off, secondary billing -- is a person's decision and
stays one.

**Visible and approved.** Every step this module takes is recorded as
`PENDING` review and puts one `CLAIM_STEP_REVIEW` item in the submitter's
queue. A step sent back stops automation on that claim until a person has
dealt with it (PD-18).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.domain.acknowledgement import (
    AcknowledgementOutcome,
    AcknowledgementSource,
    ClaimAcknowledgement,
)
from rcm.domain.claim import Claim
from rcm.domain.claim_lifecycle import transition
from rcm.domain.claim_state import FINAL_STATES, ClaimState, Guard, requires_next_action
from rcm.domain.claim_state_log import ClaimStateTransition, ReviewStatus
from rcm.domain.remittance import ClaimPayment, RemittanceAdjustment, RemittanceServiceLine
from rcm.platform.audit import ActorType, DecisionType, record_audit_event, record_decision
from rcm.platform.config import get_settings
from rcm.platform.errors import StateTransitionError, ValidationFailedError
from rcm.platform.logging import get_logger
from rcm.work import exceptions, queue
from rcm.work.models import AutomationPath, WorkItem, WorkItemKind
from rcm.work.state import OPEN_STATES, WorkItemOutcome, WorkItemState

log = get_logger(__name__)

LOGIC_VERSION: Final = "progression-v1"
ACTOR: Final = "system:claim_progression"
_SENT_BACK_KEY: Final = "step-sent-back:"

#: Before the payer: an 837 on file proves each of these happened. The
#: authorisation branch is taken when the 837 carries a prior-auth number
#: (REF*G1), because a payer only issues one when authorisation was required.
_PRE_SUBMISSION_NO_AUTH: Final[tuple[ClaimState, ...]] = (
    ClaimState.ELIGIBILITY_VERIFIED,
    ClaimState.SERVICE_RENDERED,
    ClaimState.CODED,
    ClaimState.CHARGED,
    ClaimState.SCRUBBING,
    ClaimState.READY_TO_SUBMIT,
    ClaimState.SUBMITTED,
)
_PRE_SUBMISSION_AUTH: Final[tuple[ClaimState, ...]] = (
    ClaimState.ELIGIBILITY_VERIFIED,
    ClaimState.AUTH_PENDING,
    ClaimState.AUTH_APPROVED,
    *_PRE_SUBMISSION_NO_AUTH[1:],
)

#: What a corrected submission can replace (ADR-0006, mirrors claim_state).
_REPLACEABLE: Final[frozenset[ClaimState]] = frozenset(
    {
        ClaimState.SUBMITTED,
        ClaimState.REJECTED,
        ClaimState.IN_ADJUDICATION,
        ClaimState.DENIED,
        ClaimState.PARTIALLY_PAID,
        ClaimState.PAID,
        ClaimState.CORRECTING,
    }
)


@dataclass(frozen=True, slots=True)
class Evidence:
    """What is on file for one submission. No PHI: outcomes and counts."""

    acknowledgement: AcknowledgementOutcome | None
    adjudicated: bool
    lines: int
    denied_lines: int
    replaced: bool

    def snapshot(self) -> dict[str, object]:
        return {
            "acknowledgement_277ca": str(self.acknowledgement) if self.acknowledgement else None,
            "adjudicated": self.adjudicated,
            "service_lines": self.lines,
            "denied_lines": self.denied_lines,
            "replaced": self.replaced,
        }


@dataclass(slots=True)
class ProgressionReport:
    claims_seen: int = 0
    transitions: int = 0
    reviews_opened: int = 0
    held: list[uuid.UUID] = field(default_factory=list)


def _next_action_days(state: ClaimState) -> int:
    days = get_settings().next_action_days
    return days.get(str(state), days["*"])


def _evidence(session: Session, claim: Claim) -> Evidence:
    # Matched on CLM01 alone. uq_claim_client_submitter_id makes that one
    # submission per client, and evidence is never limited by load time:
    # a 277CA or 835 can arrive before the 837 it answers.
    ack_query = select(ClaimAcknowledgement.outcome).where(
        ClaimAcknowledgement.client_id == claim.client_id,
        ClaimAcknowledgement.claim_id == claim.submitter_claim_id,
        ClaimAcknowledgement.source == str(AcknowledgementSource.CLAIM_277CA),
    )
    latest_ack = session.scalar(
        ack_query.order_by(
            ClaimAcknowledgement.received_at.desc(), ClaimAcknowledgement.created_at.desc()
        ).limit(1)
    )

    # The latest posting that is not a reversal and has not been reversed.
    # A reversed payment reopens a claim (BR-STATE-03) -- a person's call.
    reversed_ids = select(ClaimPayment.reverses_claim_payment_id).where(
        ClaimPayment.client_id == claim.client_id,
        ClaimPayment.reverses_claim_payment_id.is_not(None),
    )
    payment_query = select(ClaimPayment.claim_payment_id).where(
        ClaimPayment.client_id == claim.client_id,
        ClaimPayment.claim_id == claim.submitter_claim_id,
        ClaimPayment.is_reversal.is_(False),
        ClaimPayment.claim_payment_id.not_in(reversed_ids),
    )
    payment_id = session.scalar(payment_query.order_by(ClaimPayment.created_at.desc()).limit(1))

    lines = denied = 0
    if payment_id is not None:
        # The denial rate's definition (rcm.analytics.denials): a line is
        # denied when the payer paid nothing on it and gave a reason.
        has_reason = (
            select(RemittanceAdjustment.adjustment_id)
            .where(
                RemittanceAdjustment.service_line_id == RemittanceServiceLine.service_line_id,
                RemittanceAdjustment.client_id == claim.client_id,
            )
            .exists()
        )
        row = session.execute(
            select(
                func.count(),
                func.count().filter(RemittanceServiceLine.paid_amount == 0, has_reason),
            ).where(
                RemittanceServiceLine.client_id == claim.client_id,
                RemittanceServiceLine.claim_payment_id == payment_id,
            )
        ).one()
        lines, denied = int(row[0]), int(row[1])

    replacements = session.scalar(
        select(func.count()).where(
            Claim.client_id == claim.client_id, Claim.original_claim_id == claim.claim_id
        )
    )

    return Evidence(
        acknowledgement=AcknowledgementOutcome(latest_ack) if latest_ack else None,
        adjudicated=payment_id is not None,
        lines=lines,
        denied_lines=denied,
        replaced=bool(replacements),
    )


def _plan(claim: Claim, state: ClaimState, evidence: Evidence) -> list[tuple[ClaimState, str]]:
    """The steps the evidence supports from `state`, each with a note."""
    steps: list[tuple[ClaimState, str]] = []

    path = _PRE_SUBMISSION_AUTH if claim.prior_auth_number else _PRE_SUBMISSION_NO_AUTH
    if state is ClaimState.REGISTERED or state in path[:-1]:
        start = 0 if state is ClaimState.REGISTERED else path.index(state) + 1
        steps.extend((s, "the 837 on file shows this step was done") for s in path[start:])
        state = ClaimState.SUBMITTED

    if state is ClaimState.SUBMITTED:
        if evidence.adjudicated:
            accepted = evidence.acknowledgement is AcknowledgementOutcome.ACCEPTED
            note = (
                "277CA accepted the claim"
                if accepted
                else "inferred from the 835: no 277CA acceptance is on file"
            )
            steps.append((ClaimState.IN_ADJUDICATION, note))
            state = ClaimState.IN_ADJUDICATION
        elif evidence.acknowledgement is AcknowledgementOutcome.ACCEPTED:
            steps.append((ClaimState.IN_ADJUDICATION, "277CA accepted the claim"))
            state = ClaimState.IN_ADJUDICATION
        elif evidence.acknowledgement is AcknowledgementOutcome.REJECTED:
            steps.append((ClaimState.REJECTED, "277CA rejected the claim"))
            state = ClaimState.REJECTED

    # An 835 with no service lines says nothing line-level; the claim waits
    # in adjudication with its next action date rather than being guessed.
    if state is ClaimState.IN_ADJUDICATION and evidence.adjudicated and evidence.lines:
        if evidence.denied_lines == evidence.lines:
            outcome = ClaimState.DENIED
        elif evidence.denied_lines:
            outcome = ClaimState.PARTIALLY_PAID
        else:
            outcome = ClaimState.PAID
        steps.append((outcome, f"835: {evidence.denied_lines} of {evidence.lines} lines denied"))
        state = outcome

    if evidence.replaced and state in _REPLACEABLE:
        steps.append((ClaimState.REPLACED, "a corrected submission references this one"))

    return steps


def _held_by_a_person(session: Session, claim: Claim) -> bool:
    """A step on this claim was sent back and nobody has dealt with it yet."""
    return (
        session.scalar(
            select(WorkItem.work_item_id)
            .where(
                WorkItem.client_id == claim.client_id,
                WorkItem.claim_id == claim.claim_id,
                WorkItem.kind == str(WorkItemKind.DATA_QUALITY),
                WorkItem.source_key.startswith(_SENT_BACK_KEY),
                WorkItem.state.in_([str(s) for s in OPEN_STATES]),
            )
            .limit(1)
        )
        is not None
    )


def _open_review(session: Session, claim: Claim, now: datetime) -> WorkItem:
    return exceptions.raise_exception(
        session,
        client_id=claim.client_id,
        kind=WorkItemKind.CLAIM_STEP_REVIEW,
        problem="Automatic steps on this claim are waiting for the submitter's approval",
        record=exceptions.RecordRef("claim", claim.claim_id),
        source_key=f"claim-step-review:{claim.claim_id}",
        now=now,
        claim_id=claim.claim_id,
    )


def advance_claim(session: Session, *, claim: Claim, now: datetime) -> int:
    """Take every step the evidence supports for one claim. Returns the count."""
    state = ClaimState(claim.state)
    evidence = _evidence(session, claim)

    if state is ClaimState.REJECTED and evidence.adjudicated and not evidence.replaced:
        # The 277CA said rejected and an 835 says adjudicated. One of them is
        # wrong; guessing which would be a silent decision (PD-18).
        exceptions.raise_exception(
            session,
            client_id=claim.client_id,
            kind=WorkItemKind.DATA_QUALITY,
            problem="An 835 arrived for a claim the 277CA rejected",
            record=exceptions.RecordRef("claim", claim.claim_id),
            source_key=f"rejected-but-adjudicated:{claim.claim_id}",
            now=now,
            claim_id=claim.claim_id,
        )
        return 0

    steps = _plan(claim, state, evidence)
    for target, note in steps:
        facts = None
        if target is ClaimState.SERVICE_RENDERED and state is ClaimState.ELIGIBILITY_VERIFIED:
            facts = {str(Guard.AUTH_NOT_REQUIRED): True}
        elif target is ClaimState.AUTH_PENDING:
            facts = {str(Guard.AUTH_REQUIRED): True}
        due = (
            now.date() + timedelta(days=_next_action_days(target))
            if requires_next_action(target)
            else None
        )
        transition(
            session,
            claim_id=claim.claim_id,
            client_id=claim.client_id,
            target=target,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR,
            facts=facts,
            next_action_due=due,
            note=note,
            needs_review=True,
        )
        record_decision(
            session,
            entity_type="claim",
            entity_id=claim.claim_id,
            decision_type=DecisionType.PROGRESS,
            decided_by=ACTOR,
            logic_version=LOGIC_VERSION,
            input_snapshot={"from_state": str(state), **evidence.snapshot()},
            output={"to_state": str(target), "note": note},
        )
        state = target

    if steps:
        _open_review(session, claim, now)
    return len(steps)


def advance_all(session: Session, *, client_id: uuid.UUID, now: datetime) -> ProgressionReport:
    """Advance every claim of one client as far as its evidence goes.

    Idempotent: a second run finds no new evidence and takes no step.
    """
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    # BR-EXC-03: compliance can stop this path, and it fails closed.
    exceptions.require_automation_allowed(
        session, client_id=client_id, path=AutomationPath.CLAIM_PROGRESSION
    )

    report = ProgressionReport()
    claims: Sequence[Claim] = session.scalars(
        select(Claim)
        .where(
            Claim.client_id == client_id,
            Claim.state.not_in([str(s) for s in FINAL_STATES]),
        )
        # Originals before the submissions that replace them.
        .order_by(Claim.created_at, Claim.claim_id)
    ).all()
    for claim in claims:
        report.claims_seen += 1
        if _held_by_a_person(session, claim):
            report.held.append(claim.claim_id)
            continue
        taken = advance_claim(session, claim=claim, now=now)
        report.transitions += taken
        if taken:
            report.reviews_opened += 1

    log.info(
        "claim_progression.advanced",
        claims_seen=report.claims_seen,
        transitions=report.transitions,
        held=len(report.held),
    )
    return report


def _pending_steps(
    session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID
) -> list[ClaimStateTransition]:
    return list(
        session.scalars(
            select(ClaimStateTransition)
            .where(
                ClaimStateTransition.client_id == client_id,
                ClaimStateTransition.claim_id == claim_id,
                ClaimStateTransition.review_status == str(ReviewStatus.PENDING),
            )
            .with_for_update()
        ).all()
    )


def approve_steps(
    session: Session,
    *,
    claim_id: uuid.UUID,
    client_id: uuid.UUID,
    operator: str,
    now: datetime,
) -> int:
    """The submitter approves every step waiting on this claim.

    Returns how many were approved. A retry finds none and changes nothing
    (PD-14). Steps taken after this call wait for their own approval.
    """
    steps = _pending_steps(session, claim_id=claim_id, client_id=client_id)
    for step in steps:
        step.review_status = str(ReviewStatus.APPROVED)
        step.reviewed_by = operator
        step.reviewed_at = now
    if steps:
        record_audit_event(
            session,
            actor_type=ActorType.USER,
            actor_id=operator,
            action="claim.steps_approved",
            entity_type="claim",
            entity_id=claim_id,
            after_state={"approved": [str(s.transition_id) for s in steps]},
        )

    review = session.scalar(
        select(WorkItem).where(
            WorkItem.client_id == client_id,
            WorkItem.claim_id == claim_id,
            WorkItem.kind == str(WorkItemKind.CLAIM_STEP_REVIEW),
            WorkItem.state.in_([str(s) for s in OPEN_STATES]),
        )
    )
    if review is not None:
        # Approving is working the item: take it, open it, record the outcome.
        # Held by someone else, `claim` refuses by name rather than taking it.
        item_id = review.work_item_id
        if review.assigned_to != operator:
            queue.claim(
                session, work_item_id=item_id, client_id=client_id, operator=operator, now=now
            )
        if review.state == str(WorkItemState.ASSIGNED):
            queue.open_item(
                session, work_item_id=item_id, client_id=client_id, operator=operator, now=now
            )
        queue.resolve(
            session,
            work_item_id=item_id,
            client_id=client_id,
            operator=operator,
            outcome=WorkItemOutcome.NO_ACTION_NEEDED,
            now=now,
            note="automatic steps approved",
        )
    session.flush()
    return len(steps)


def send_back_step(
    session: Session,
    *,
    transition_id: uuid.UUID,
    client_id: uuid.UUID,
    operator: str,
    reason: str,
    now: datetime,
) -> WorkItem:
    """The submitter disputes one automatic step.

    The step is marked, a person is asked to correct the claim, and
    automation leaves the claim alone until they have (PD-18).
    """
    if not reason.strip():
        raise ValidationFailedError(field="reason", reason="say why the step is wrong")
    step = session.scalar(
        select(ClaimStateTransition)
        .where(
            ClaimStateTransition.transition_id == transition_id,
            ClaimStateTransition.client_id == client_id,
        )
        .with_for_update()
    )
    if step is None:
        raise ValidationFailedError(field="transition_id", reason="no such step for this client")
    if step.review_status != str(ReviewStatus.PENDING):
        raise StateTransitionError(
            source=step.review_status,
            target=str(ReviewStatus.SENT_BACK),
            reason="only a step waiting for review can be sent back",
        )

    step.review_status = str(ReviewStatus.SENT_BACK)
    step.reviewed_by = operator
    step.reviewed_at = now
    step.review_note = reason
    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=operator,
        action="claim.step_sent_back",
        entity_type="claim",
        entity_id=step.claim_id,
        before_state={"review_status": str(ReviewStatus.PENDING)},
        after_state={"review_status": str(ReviewStatus.SENT_BACK), "step": str(transition_id)},
    )
    item = exceptions.raise_exception(
        session,
        client_id=client_id,
        kind=WorkItemKind.DATA_QUALITY,
        problem=(
            f"The submitter sent back the automatic step {step.source_state} -> {step.target_state}"
        ),
        record=exceptions.RecordRef("claim", step.claim_id),
        source_key=f"{_SENT_BACK_KEY}{transition_id}",
        now=now,
        claim_id=step.claim_id,
    )
    session.flush()
    return item
