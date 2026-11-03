"""Claims move through the state machine on their own; the submitter approves.

Owner decision (2026-11-01): "everything done automatically but visible to
the user who submitted the claim, so that he can review it and approve the
system's steps." ADR-0006.

**What moves a claim.** Only evidence already on file:

- an 837 we hold was sent, so every step before `Submitted` is inferred,
  and recorded as inferred;
- a 277CA verdict accepts (`InAdjudication`) or rejects (`Rejected`, PD-4);
- an 835 adjudicated it, so it was accepted even without a 277CA -- and
  the history says the acceptance was inferred rather than pretending a
  277CA arrived;
- a later submission chained to it by REF*F8 replaces it, once the payer
  has taken that submission.

Evidence that contradicts itself, or that this module will not read (two
835 postings, a line paid nothing for patient-responsibility reasons only,
a void), raises a named DATA_QUALITY exception and the claim waits (PD-18).

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
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from rcm.analytics.denials import refusal_clause
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

#: Before the payer: an 837 on file is what these steps leave behind, so they
#: are inferred from it and recorded as inferred. The authorisation branch is
#: taken when the 837 carries a prior-auth number (REF*G1), because a payer
#: only issues one when authorisation was required.
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


#: A replacement replaces only once the payer has taken it. Until then the
#: original is still the live claim at the payer, and ending its follow-up
#: would lose it if the replacement is rejected.
_TAKEN_BY_PAYER: Final[frozenset[ClaimState]] = frozenset(
    {
        ClaimState.IN_ADJUDICATION,
        ClaimState.PAID,
        ClaimState.PARTIALLY_PAID,
        ClaimState.DENIED,
        ClaimState.APPEALING,
        ClaimState.CORRECTING,
        ClaimState.WRITTEN_OFF,
        ClaimState.SECONDARY_BILLING,
        ClaimState.PATIENT_BALANCE,
        ClaimState.CLOSED,
        ClaimState.REOPENED,
        ClaimState.REPLACED,
    }
)

#: CLM05-3 frequency 8: void. A void ends the claim at the payer and may bring
#: a recoupment; telling it apart from a replacement is M4's (ADR-0006).
_VOID: Final = "8"


@dataclass(frozen=True, slots=True)
class Problem:
    """Evidence that contradicts itself, or that this module will not read.

    Raised as a named DATA_QUALITY exception, never guessed past (PD-18).
    """

    key: str
    text: str


REJECTED_BUT_ADJUDICATED: Final = Problem(
    "rejected-but-adjudicated", "An 835 arrived for a claim the 277CA rejected"
)
ACCEPTED_AFTER_REJECTION: Final = Problem(
    "accepted-after-rejection",
    "A 277CA accepted a claim it rejected earlier; confirm the claim was resent",
)
SEVERAL_POSTINGS: Final = Problem(
    "several-835-postings",
    "More than one 835 posting is on file for this claim (split or secondary); "
    "a person sets the outcome",
)
NO_SERVICE_LINES: Final = Problem(
    "835-without-service-lines",
    "The 835 for this claim has no service lines, so the outcome cannot be read per line",
)
PATIENT_SHARE_ONLY: Final = Problem(
    "835-patient-share-only-line",
    "An 835 line was paid nothing with no refusal reason (the patient's cost share "
    "or a payment adjustment took it all); a person sets the outcome",
)
VOIDED: Final = Problem(
    "void-received", "A void was sent for this claim; a person handles voids until M4"
)
VOID_SUBMISSION: Final = Problem(
    "void-submission", "This submission is a void; a person handles voids until M4"
)
REJECTED_AFTER_ACCEPTANCE: Final = Problem(
    "rejected-after-acceptance",
    "A 277CA rejected a claim the payer had already accepted",
)

#: States a claim reaches only once the payer has accepted it. A later 277CA
#: rejection contradicts every one of them.
_ACCEPTED: Final[frozenset[ClaimState]] = frozenset(
    {
        ClaimState.IN_ADJUDICATION,
        ClaimState.PAID,
        ClaimState.PARTIALLY_PAID,
        ClaimState.DENIED,
    }
)


@dataclass(frozen=True, slots=True)
class Evidence:
    """What is on file for one submission. No PHI: outcomes and counts."""

    acknowledgement: AcknowledgementOutcome | None
    postings: int
    lines: int
    denied_lines: int
    patient_share_only_lines: int
    replaced: bool
    voided: bool

    @property
    def adjudicated(self) -> bool:
        return self.postings > 0

    def snapshot(self) -> dict[str, object]:
        return {
            "acknowledgement_277ca": str(self.acknowledgement) if self.acknowledgement else None,
            "postings_835": self.postings,
            "service_lines": self.lines,
            "denied_lines": self.denied_lines,
            "patient_share_only_lines": self.patient_share_only_lines,
            "replaced": self.replaced,
            "voided": self.voided,
        }


@dataclass(slots=True)
class ProgressionReport:
    claims_seen: int = 0
    transitions: int = 0
    reviews_opened: int = 0
    problems_raised: int = 0
    held: list[uuid.UUID] = field(default_factory=list)


def _next_action_days(state: ClaimState) -> int:
    days = get_settings().next_action_days
    return days.get(str(state), days["*"])


def _evidence(session: Session, claim: Claim) -> Evidence:
    # Matched on CLM01 alone. uq_claim_client_submitter_id makes that one
    # submission per client, and evidence is never limited by load time:
    # a 277CA or 835 can arrive before the 837 it answers.
    latest_ack = session.scalar(
        select(ClaimAcknowledgement.outcome)
        .where(
            ClaimAcknowledgement.client_id == claim.client_id,
            ClaimAcknowledgement.claim_id == claim.submitter_claim_id,
            ClaimAcknowledgement.source == str(AcknowledgementSource.CLAIM_277CA),
        )
        .order_by(ClaimAcknowledgement.received_at.desc(), ClaimAcknowledgement.created_at.desc())
        .limit(1)
    )

    # Postings that are not reversals and have not been reversed. A reversed
    # payment reopens a claim (BR-STATE-03) -- a person's call. Two or more
    # live postings (a split claim, or a secondary payer's 835 for the same
    # CLM01) are not summed or picked between: the outcome is held. CLP02
    # cannot separate primary from secondary yet, because the synthetic
    # generator writes 2 on primary claims that carry a patient share.
    reversal = aliased(ClaimPayment)
    postings = session.scalars(
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
        .limit(2)
    ).all()

    lines = denied = patient_share_only = 0
    if len(postings) == 1:
        # The denial rule (rcm.analytics.denials, ADR-0012): the payer paid
        # nothing on the line and gave a refusal -- any reason but the
        # patient's cost share or a payment adjustment. A line paid nothing
        # without one (a deductible swallowing the allowed amount, beside
        # CO-45) was not refused; it is counted apart and held for a person
        # rather than called paid or denied.
        any_reason = (
            select(RemittanceAdjustment.adjustment_id)
            .where(
                RemittanceAdjustment.service_line_id == RemittanceServiceLine.service_line_id,
                RemittanceAdjustment.client_id == claim.client_id,
            )
            .exists()
        )
        payer_reason = (
            select(RemittanceAdjustment.adjustment_id)
            .where(
                RemittanceAdjustment.service_line_id == RemittanceServiceLine.service_line_id,
                RemittanceAdjustment.client_id == claim.client_id,
                refusal_clause(),
            )
            .exists()
        )
        zero_paid = RemittanceServiceLine.paid_amount == 0
        row = session.execute(
            select(
                func.count(),
                func.count().filter(zero_paid, payer_reason),
                func.count().filter(zero_paid, any_reason, ~payer_reason),
            ).where(
                RemittanceServiceLine.client_id == claim.client_id,
                RemittanceServiceLine.claim_payment_id == postings[0],
            )
        ).one()
        lines, denied, patient_share_only = int(row[0]), int(row[1]), int(row[2])

    replacements = session.execute(
        select(Claim.state, Claim.claim_frequency_code).where(
            Claim.client_id == claim.client_id, Claim.original_claim_id == claim.claim_id
        )
    ).all()

    return Evidence(
        acknowledgement=AcknowledgementOutcome(latest_ack) if latest_ack else None,
        postings=len(postings),
        lines=lines,
        denied_lines=denied,
        patient_share_only_lines=patient_share_only,
        replaced=any(
            ClaimState(state) in _TAKEN_BY_PAYER and frequency != _VOID
            for state, frequency in replacements
        ),
        voided=any(frequency == _VOID for _, frequency in replacements),
    )


def _plan(
    claim: Claim, state: ClaimState, evidence: Evidence
) -> tuple[list[tuple[ClaimState, str]], list[Problem]]:
    """The steps the evidence supports from `state`, and what stops it."""
    steps: list[tuple[ClaimState, str]] = []
    problems: list[Problem] = []

    path = _PRE_SUBMISSION_AUTH if claim.prior_auth_number else _PRE_SUBMISSION_NO_AUTH
    if state is ClaimState.REGISTERED or state in path[:-1]:
        # Inferred, not observed: an 837 is what these steps leave behind,
        # not a record of them, and the note says so (PD-1).
        start = 0 if state is ClaimState.REGISTERED else path.index(state) + 1
        steps.extend((s, "inferred from the 837 on file, not observed") for s in path[start:])
        state = ClaimState.SUBMITTED

    if claim.claim_frequency_code == _VOID:
        # Sent, so walked to Submitted, and no further: a void is not a claim
        # to adjudicate, and an 835 on its CLM01 must not make it Paid.
        return steps, [VOID_SUBMISSION]

    if evidence.acknowledgement is AcknowledgementOutcome.REJECTED:
        if evidence.adjudicated and state in (
            ClaimState.SUBMITTED,
            ClaimState.REJECTED,
            ClaimState.IN_ADJUDICATION,
        ):
            # The 277CA said rejected and an 835 says adjudicated. One of
            # them is wrong; guessing which would be a silent decision
            # (PD-18). A replacement does not settle it either.
            return steps, [REJECTED_BUT_ADJUDICATED]
        if state in _ACCEPTED:
            return steps, [REJECTED_AFTER_ACCEPTANCE]

    if state is ClaimState.REJECTED and evidence.acknowledgement is AcknowledgementOutcome.ACCEPTED:
        # No automatic way back from Rejected: a person resent it, and a
        # person says so.
        problems.append(ACCEPTED_AFTER_REJECTION)

    if state is ClaimState.SUBMITTED:
        if evidence.adjudicated:
            accepted = evidence.acknowledgement is AcknowledgementOutcome.ACCEPTED
            note = (
                "277CA accepted the claim"
                if accepted
                else "inferred from the 835: no 277CA verdict is on file"
            )
            steps.append((ClaimState.IN_ADJUDICATION, note))
            state = ClaimState.IN_ADJUDICATION
        elif evidence.acknowledgement is AcknowledgementOutcome.ACCEPTED:
            steps.append((ClaimState.IN_ADJUDICATION, "277CA accepted the claim"))
            state = ClaimState.IN_ADJUDICATION
        elif evidence.acknowledgement is AcknowledgementOutcome.REJECTED:
            steps.append((ClaimState.REJECTED, "277CA rejected the claim"))
            state = ClaimState.REJECTED

    if state is ClaimState.IN_ADJUDICATION and evidence.adjudicated:
        # Each problem leaves the claim in adjudication, with its next action
        # date, and puts the 835 in front of a person.
        if evidence.postings > 1:
            problems.append(SEVERAL_POSTINGS)
        elif not evidence.lines:
            problems.append(NO_SERVICE_LINES)
        elif evidence.patient_share_only_lines:
            problems.append(PATIENT_SHARE_ONLY)
        else:
            if evidence.denied_lines == evidence.lines:
                outcome = ClaimState.DENIED
            elif evidence.denied_lines:
                outcome = ClaimState.PARTIALLY_PAID
            else:
                outcome = ClaimState.PAID
            steps.append(
                (outcome, f"835: {evidence.denied_lines} of {evidence.lines} lines denied")
            )
            state = outcome

    if evidence.voided:
        problems.append(VOIDED)
    elif evidence.replaced and state in _REPLACEABLE:
        steps.append((ClaimState.REPLACED, "a corrected submission the payer took replaces it"))

    return steps, problems


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


def _lock_claim(session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID) -> Claim:
    """The claim, locked and re-read.

    Progression, approval and send-back on one claim take turns, and each
    works from the state as it is now rather than as it was when listed.
    """
    claim = session.scalar(
        select(Claim)
        .where(Claim.claim_id == claim_id, Claim.client_id == client_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if claim is None:
        raise ValidationFailedError(field="claim_id", reason="no such claim for this client")
    return claim


@dataclass(frozen=True, slots=True)
class Advanced:
    steps: int = 0
    problems: int = 0
    #: A step was sent back and nobody has dealt with it: left alone.
    held: bool = False


def _problem_open(session: Session, claim: Claim, problem: Problem) -> bool:
    return (
        session.scalar(
            select(WorkItem.work_item_id)
            .where(
                WorkItem.client_id == claim.client_id,
                WorkItem.kind == str(WorkItemKind.DATA_QUALITY),
                WorkItem.source_key == f"{problem.key}:{claim.claim_id}",
                WorkItem.state.in_([str(s) for s in OPEN_STATES]),
            )
            .limit(1)
        )
        is not None
    )


def advance_claim(session: Session, *, claim: Claim, now: datetime) -> Advanced:
    """Take every step the evidence supports for one claim."""
    claim = _lock_claim(session, claim_id=claim.claim_id, client_id=claim.client_id)
    # Under the lock: a send-back that landed after the claim was listed
    # must still stop it (PD-18).
    if _held_by_a_person(session, claim):
        return Advanced(held=True)
    state = ClaimState(claim.state)
    evidence = _evidence(session, claim)
    steps, problems = _plan(claim, state, evidence)

    for target, note in steps:
        facts = None
        if target is ClaimState.SERVICE_RENDERED and state is ClaimState.ELIGIBILITY_VERIFIED:
            # The machine needs the guard. The 837 carrying no prior-auth
            # number is all it rests on, and the decision record says so.
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
            input_snapshot={
                "from_state": str(state),
                "inferred": note.startswith("inferred"),
                "prior_auth_number_on_837": bool(claim.prior_auth_number),
                **evidence.snapshot(),
            },
            output={"to_state": str(target), "note": note},
        )
        state = target

    for problem in problems:
        if not _problem_open(session, claim, problem):
            # A hold is a decision like a step (PD-1). Recorded when the item
            # is raised, not again on every run that finds it still open.
            record_decision(
                session,
                entity_type="claim",
                entity_id=claim.claim_id,
                decision_type=DecisionType.PROGRESS,
                decided_by=ACTOR,
                logic_version=LOGIC_VERSION,
                input_snapshot={"from_state": str(state), **evidence.snapshot()},
                output={"held": problem.key, "state": str(state)},
            )
        exceptions.raise_exception(
            session,
            client_id=claim.client_id,
            kind=WorkItemKind.DATA_QUALITY,
            problem=problem.text,
            record=exceptions.RecordRef("claim", claim.claim_id),
            source_key=f"{problem.key}:{claim.claim_id}",
            now=now,
            claim_id=claim.claim_id,
        )

    if steps:
        _open_review(session, claim, now)
    return Advanced(steps=len(steps), problems=len(problems))


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
        # Replacements before the claims they replace: an original is only
        # Replaced once the payer has taken its replacement, so that one must
        # move first for a single run to be enough.
        .order_by(Claim.created_at.desc(), Claim.claim_id.desc())
    ).all()
    for claim in claims:
        report.claims_seen += 1
        outcome = advance_claim(session, claim=claim, now=now)
        if outcome.held:
            report.held.append(claim.claim_id)
        report.transitions += outcome.steps
        report.problems_raised += outcome.problems
        if outcome.steps:
            report.reviews_opened += 1

    log.info(
        "claim_progression.advanced",
        claims_seen=report.claims_seen,
        transitions=report.transitions,
        problems=report.problems_raised,
        held=len(report.held),
    )
    return report


def _steps_of(
    session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID
) -> list[ClaimStateTransition]:
    return list(
        session.scalars(
            select(ClaimStateTransition)
            .where(
                ClaimStateTransition.client_id == client_id,
                ClaimStateTransition.claim_id == claim_id,
            )
            .with_for_update()
        ).all()
    )


def approve_steps(
    session: Session,
    *,
    claim_id: uuid.UUID,
    client_id: uuid.UUID,
    step_ids: Collection[uuid.UUID],
    operator: str,
    now: datetime,
) -> int:
    """The submitter approves the steps they were shown.

    Only `step_ids`: a step taken while the review was open was never seen,
    so it is not approved under their name, and the review stays open for
    it. Returns how many were approved. A retry finds them approved already
    and changes nothing (PD-14).
    """
    if not step_ids:
        raise ValidationFailedError(field="step_ids", reason="name the steps being approved")
    _lock_claim(session, claim_id=claim_id, client_id=client_id)
    on_claim = _steps_of(session, claim_id=claim_id, client_id=client_id)
    if not set(step_ids) <= {s.transition_id for s in on_claim}:
        raise ValidationFailedError(field="step_ids", reason="a step named is not on this claim")
    pending = [s for s in on_claim if s.review_status == str(ReviewStatus.PENDING)]
    steps = [s for s in pending if s.transition_id in step_ids]
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
    if review is not None and len(steps) == len(pending):
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
    # The claim before the step, the order progression and approval lock in,
    # so the three never wait on each other in a circle.
    claim_id = session.scalar(
        select(ClaimStateTransition.claim_id).where(
            ClaimStateTransition.transition_id == transition_id,
            ClaimStateTransition.client_id == client_id,
        )
    )
    if claim_id is None:
        raise ValidationFailedError(field="transition_id", reason="no such step for this client")
    _lock_claim(session, claim_id=claim_id, client_id=client_id)
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
