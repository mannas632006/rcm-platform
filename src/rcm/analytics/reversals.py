"""Which claims the payer took money back on, and how (M1.10).

Two mechanisms, and they are not the same event:

    reversal     CLP02 = 22. The payer re-adjudicates a claim it already paid
                 and sends a later remittance negating the original posting.
                 The claim's outcome changed retroactively.

    recoupment   A PLB entry with reason WO. The payer keeps the original
                 adjudication and instead reduces a *later* cheque to recover
                 the money (EC-47). The claim's outcome is unchanged; the
                 cash is taken back somewhere else.

Both reopen the claim -- DOC-02's state machine has `Closed --> Reopened:
reversal/recoupment`, and BR-STATE-03 says outright that *"`Closed` is not
final"*. This module reports which claims are in that position. Driving the
state transition is M3.1's job, and BR-STATE-01 requires it to be logged with
a timestamp, an actor and a reason, none of which belong in a read query.

The distinction matters for what you do next, which is why they are counted
separately rather than summed. A reversal means the adjudication was wrong and
the claim may need reworking; a recoupment means the adjudication stood and
the money moved. Collapsing them loses the only information that tells an
operator which it is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, exists, func, select
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim
from rcm.domain.remittance import ClaimPayment, ProviderAdjustment


@dataclass(frozen=True, slots=True)
class ReopenedClaims:
    """How many distinct claims were reopened, by mechanism.

    Counted at claim grain rather than posting grain, for the same reason the
    denial rate is: a claim reversed twice is one reopened claim, and counting
    postings would report two.
    """

    reversed_claims: int
    recouped_claims: int
    total: int


def reopened_claim_ids(session: Session, *, client_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    """Root claim ids with a reversal or a recoupment against them.

    Root ids, so a claim reopened on its third submission is reported once,
    under the claim rather than the attempt (BR-DATA-03).
    """
    rows = session.scalars(
        select(Claim.root_claim_id)
        .where(Claim.client_id == client_id, _reopened(client_id))
        .distinct()
    ).all()
    return tuple(rows)


def reopened_claims(session: Session, *, client_id: uuid.UUID) -> ReopenedClaims:
    """Counts of reopened claims, split by why."""
    reversed_count = session.scalar(
        select(func.count(Claim.root_claim_id.distinct())).where(
            Claim.client_id == client_id, _has_reversal(client_id)
        )
    )
    recouped_count = session.scalar(
        select(func.count(Claim.root_claim_id.distinct())).where(
            Claim.client_id == client_id, _has_recoupment(client_id)
        )
    )
    total = session.scalar(
        select(func.count(Claim.root_claim_id.distinct())).where(
            Claim.client_id == client_id, _reopened(client_id)
        )
    )
    return ReopenedClaims(
        reversed_claims=reversed_count or 0,
        recouped_claims=recouped_count or 0,
        total=total or 0,
    )


def _has_reversal(client_id: uuid.UUID) -> ColumnElement[bool]:
    """A reversal posting exists for this claim's submitted claim number."""
    return exists().where(
        and_(
            ClaimPayment.client_id == client_id,
            ClaimPayment.claim_id == Claim.submitter_claim_id,
            ClaimPayment.is_reversal.is_(True),
        )
    )


def _has_recoupment(client_id: uuid.UUID) -> ColumnElement[bool]:
    """A PLB entry claws back a payment made on this claim."""
    return exists().where(
        and_(
            ClaimPayment.client_id == client_id,
            ClaimPayment.claim_id == Claim.submitter_claim_id,
            exists()
            .where(
                and_(
                    ProviderAdjustment.recoups_claim_payment_id == ClaimPayment.claim_payment_id,
                    ProviderAdjustment.client_id == client_id,
                )
            )
            .correlate(ClaimPayment),
        )
    )


def _reopened(client_id: uuid.UUID) -> ColumnElement[bool]:
    return _has_reversal(client_id) | _has_recoupment(client_id)
