"""Find a claim by its number or its payer, for the top bar's search (S-01).

The search box has promised "Search claim ID or payer" since M5.1, and
the screen behind it said it was not available. Looking a claim up is how
most payer calls and patient questions start, so GATE M5 counts it among
the highest-volume journeys (ADR-0022).

- **What matches:** a claim number starting with the text (case ignored),
  or a payer whose id is the text or whose name contains it.
- **Scope:** through `scoped_query` (PD-20). Another client's claim is
  never a result, not even a count.
- **Each submission is listed** with its attempt number. A resubmission is
  the same claim (PD-7), and the operator picks the one they mean.
- **The text is matched, never interpreted.** LIKE wildcards in it are
  escaped, and it is never logged: a search can be a member's name typed
  in the wrong box (PD-13).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim
from rcm.domain.payer import Payer
from rcm.platform.auth import Principal, scoped_query
from rcm.platform.errors import ValidationFailedError

MIN_QUERY: Final = 2
MAX_QUERY: Final = 80
MAX_RESULTS: Final = 50


@dataclass(frozen=True, slots=True)
class ClaimHit:
    claim_id: uuid.UUID
    submitter_claim_id: str
    submission_attempt: int
    state: str
    payer_id: str
    payer_name: str | None
    date_of_service: date | None
    total_charge: Decimal


@dataclass(frozen=True, slots=True)
class ClaimSearch:
    hits: list[ClaimHit]
    #: More matched than are listed: the operator should narrow the search.
    truncated: bool


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_claims(
    session: Session, *, principal: Principal, q: str, limit: int = MAX_RESULTS
) -> ClaimSearch:
    text = q.strip()
    if not MIN_QUERY <= len(text) <= MAX_QUERY:
        raise ValidationFailedError(
            field="q", reason=f"between {MIN_QUERY} and {MAX_QUERY} characters"
        )
    if not 1 <= limit <= MAX_RESULTS:
        raise ValidationFailedError(field="limit", reason=f"between 1 and {MAX_RESULTS}")
    pattern = _like(text)
    statement = (
        select(Claim, Payer.name)
        .outerjoin(Payer, Payer.payer_id == Claim.payer_id)
        .where(
            or_(
                func.lower(Claim.submitter_claim_id).like(f"{pattern.lower()}%", escape="\\"),
                Claim.payer_id == text,
                func.lower(Payer.name).like(f"%{pattern.lower()}%", escape="\\"),
            )
        )
    )
    rows = session.execute(
        scoped_query(statement, Claim, principal)
        .order_by(
            Claim.date_of_service.desc().nulls_last(),
            Claim.submitter_claim_id,
            Claim.submission_attempt,
            Claim.claim_id,
        )
        .limit(limit + 1)
    ).all()
    hits = [
        ClaimHit(
            claim_id=claim.claim_id,
            submitter_claim_id=claim.submitter_claim_id,
            submission_attempt=claim.submission_attempt,
            state=claim.state,
            payer_id=claim.payer_id,
            payer_name=name,
            date_of_service=claim.date_of_service,
            total_charge=claim.total_charge,
        )
        for claim, name in rows[:limit]
    ]
    return ClaimSearch(hits=hits, truncated=len(rows) > limit)
