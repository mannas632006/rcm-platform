"""Moving a claim through the state machine, durably (M3.2).

    FR-BE-10 (MUST): Transitions occur inside a database transaction with
    row-level locking. Concurrent transitions must not interleave.

    FR-BE-12 (MUST): The audit write occurs inside the same transaction as
    the data change. An audit log that can diverge from the data it
    describes is not an audit log.

`claim_state.py` declares which transitions exist; this module performs
them. The separation is deliberate -- the table is a statement about the
domain and should be readable without a database in mind, while everything
here is about the two ways a correct transition can still go wrong.

**Two operators, one claim.** A denial arrives while an operator is writing
off the same claim. Both read `Denied`, both decide, and both write. Without
a lock the second write lands on a claim that is no longer in the state it
was validated against, and the loser's transition is recorded as though it
had been applied to the state the winner left behind. `SELECT ... FOR
UPDATE` makes the second transaction wait, re-read, and validate against
what is actually there -- which usually means it is correctly refused.

**A transition nobody can see.** The state change, its history row and its
audit event are one write or none. Splitting them produces a claim whose
state no record explains, which is unreconcilable rather than merely untidy
(PD-2).

**BR-STATE-04 is enforced here, not hoped for.** A transition into a state a
claim cannot rest in must supply the date of the next action, and the call
is refused without one. A claim sitting in `AuthPending` for six weeks
because nobody scheduled a follow-up is the commonest way money is lost
quietly, and the only reliable moment to require the date is the moment the
claim enters the state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim
from rcm.domain.claim_state import (
    ClaimState,
    Transition,
    check,
    is_final,
    requires_next_action,
)
from rcm.domain.claim_state_log import ClaimStateTransition
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.errors import ResourceNotFoundError, StateTransitionError
from rcm.platform.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """What happened, for the caller and for the record."""

    claim_id: uuid.UUID
    source: ClaimState
    target: ClaimState
    reason: str
    next_action_due: date | None
    recorded_at: datetime


def current_state(session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID) -> ClaimState:
    """The claim's state, without taking a lock.

    For reading. Anything that intends to *change* the state must go through
    `transition`, which locks first -- a state read outside the lock is
    already stale by the time the caller acts on it.
    """
    claim = session.scalar(
        select(Claim).where(Claim.claim_id == claim_id, Claim.client_id == client_id)
    )
    if claim is None:
        raise ResourceNotFoundError(entity="claim", entity_id=str(claim_id))
    return ClaimState(claim.state)


def transition(
    session: Session,
    *,
    claim_id: uuid.UUID,
    client_id: uuid.UUID,
    target: ClaimState,
    actor_type: ActorType,
    actor_id: str,
    facts: dict[str, bool] | None = None,
    next_action_due: date | None = None,
    note: str = "",
) -> TransitionResult:
    """Move one claim to `target`, or refuse and explain why.

    The whole operation happens in the caller's transaction: lock, validate,
    apply, record, audit. A caller that commits gets all of it; a caller
    that rolls back gets none of it.
    """
    # PD-20: the client is part of the lookup, not a filter applied after.
    # A claim id alone must never be enough to move another tenant's claim.
    claim = session.scalar(
        select(Claim)
        .where(Claim.claim_id == claim_id, Claim.client_id == client_id)
        .with_for_update()
    )
    if claim is None:
        raise ResourceNotFoundError(entity="claim", entity_id=str(claim_id))

    source = ClaimState(claim.state)

    # Validation happens *after* the lock and against the row just read, not
    # against whatever the caller believed when it decided. That ordering is
    # the entire protection: a concurrent writer that got here first has
    # already committed, and this re-read sees its result.
    defined: Transition = check(source, target, facts=facts)

    if requires_next_action(target) and next_action_due is None:
        raise StateTransitionError(
            source=str(source),
            target=str(target),
            reason="BR-STATE-04: a claim in this state needs a scheduled next action",
        )

    # A resting state has nothing pending. Carrying a stale date forward
    # would leave closed claims appearing on work queues forever.
    resolved_due = None if not requires_next_action(target) else next_action_due

    claim.state = str(target)
    claim.next_action_due = resolved_due

    record = ClaimStateTransition(
        client_id=client_id,
        claim_id=claim_id,
        source_state=str(source),
        target_state=str(target),
        # BR-STATE-01: the reason is recorded, and it is the domain's
        # sentence rather than the pair of state names, which an operator
        # reading a claim's history can already see.
        reason=defined.reason,
        note=note,
        actor_type=str(actor_type),
        actor_id=actor_id,
        guard=str(defined.guard) if defined.guard else "",
        next_action_due=resolved_due,
    )
    session.add(record)

    record_audit_event(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="claim.transitioned",
        entity_type="claim",
        entity_id=claim_id,
        before_state={"state": str(source)},
        after_state={"state": str(target), "reason": defined.reason},
    )
    session.flush()

    # PD-13: states and reasons are domain vocabulary, not PHI. The claim id
    # is a surrogate key, and no identifier of a person appears here.
    log.info(
        "claim.transitioned",
        source=str(source),
        target=str(target),
        reason=defined.reason,
        is_final=is_final(target),
    )

    return TransitionResult(
        claim_id=claim_id,
        source=source,
        target=target,
        reason=defined.reason,
        next_action_due=resolved_due,
        recorded_at=record.occurred_at or datetime.now(tz=None).astimezone(),
    )


def history(
    session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID
) -> list[ClaimStateTransition]:
    """Every transition this claim has made, oldest first.

    BR-STATE-01's purpose: a reviewer asking "how did this claim get here"
    reads a list rather than reconstructing it from remittances.
    """
    return list(
        session.scalars(
            select(ClaimStateTransition)
            .where(
                ClaimStateTransition.claim_id == claim_id,
                ClaimStateTransition.client_id == client_id,
            )
            .order_by(ClaimStateTransition.occurred_at, ClaimStateTransition.transition_id)
        ).all()
    )


def claims_needing_action(
    session: Session, *, client_id: uuid.UUID, as_of: date
) -> list[dict[str, Any]]:
    """Claims whose next action is due, and claims that have none.

    BR-STATE-04 says no claim may *remain* in a non-terminal state without a
    scheduled next action. The second half of this query is the one that
    matters: a claim with a null due date in a state that requires one is
    not merely overdue, it has fallen out of the process entirely and
    nothing will ever surface it again.
    """
    resting = [str(s) for s in ClaimState if not requires_next_action(s)]
    rows = session.execute(
        select(Claim.claim_id, Claim.submitter_claim_id, Claim.state, Claim.next_action_due)
        .where(
            Claim.client_id == client_id,
            Claim.state.notin_(resting),
            (Claim.next_action_due.is_(None)) | (Claim.next_action_due <= as_of),
        )
        .order_by(Claim.next_action_due.nulls_first(), Claim.claim_id)
    ).all()

    return [
        {
            "claim_id": row.claim_id,
            "submitter_claim_id": row.submitter_claim_id,
            "state": row.state,
            "next_action_due": row.next_action_due,
            "unscheduled": row.next_action_due is None,
        }
        for row in rows
    ]
