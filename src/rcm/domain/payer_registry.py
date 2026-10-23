"""Recording what a payer is called, and proposing when two are one (M1.7).

Three operations, and the separation between them is the design.

    record_alias        every spelling a file used, against the identifier
                        that file carried. Observation, not judgement.
    resolve_by_name     look a payer up by name, using **confirmed** aliases
                        only. A suggestion is not an answer.
    propose_matches     notice that two identifiers look like one payer and
                        write a review item. A proposal, never an action.

`record_alias` is deliberately permissive and `resolve_by_name` deliberately
strict, because they carry opposite risks. Recording a spelling that turns out
to belong to another payer costs a row a human can reject. Resolving a claim
through an unconfirmed alias silently attributes money to the wrong payer, and
nothing downstream can tell.

Nothing here merges payers. PD-8 keys identity on `payer_id`, and a merge is a
change of identity -- the SRS asks for *"Manual review UI for ambiguous
matches"* and this is the queue behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.domain.payer import MatchCandidateStatus, Payer, PayerAlias, PayerMatchCandidate
from rcm.domain.payer_matching import REVIEW_THRESHOLD, normalise_payer_name, similarity
from rcm.platform.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AliasOutcome:
    """What recording one spelling did."""

    #: The alias row's payer, which is not always the one just offered: a
    #: spelling already claimed by another payer keeps its original owner.
    payer_id: str
    normalised: str
    created: bool
    #: True when this spelling already belonged to a *different* payer. The
    #: alias is left alone and a review candidate is raised instead, because
    #: one spelling resolving to two payers makes resolution depend on row
    #: order -- which is what the uniqueness constraint exists to prevent.
    conflicted: bool = False


def record_alias(session: Session, *, payer_id: str, name: str) -> AliasOutcome | None:
    """Record that `payer_id` was seen under `name`.

    Returns `None` for a name that normalises to nothing -- an empty `N1*PR`
    is common and is not evidence of anything.

    The alias is written unconfirmed. It is a record of what arrived, and a
    human decides whether it is a name this payer should be found by.
    """
    normalised = normalise_payer_name(name)
    if not normalised:
        return None

    existing = session.scalar(select(PayerAlias).where(PayerAlias.normalised_alias == normalised))
    if existing is not None:
        if existing.payer_id != payer_id:
            # One spelling, two payers. Do not overwrite and do not guess:
            # raise it for review and leave the first claim standing.
            _propose(
                session,
                payer_id,
                existing.payer_id,
                matched_on=name.strip(),
                confidence=Decimal("1.000"),
            )
            log.warning(
                "payer.alias_conflict",
                normalised_alias=normalised,
                claimed_by=existing.payer_id,
                also_seen_for=payer_id,
            )
            return AliasOutcome(
                payer_id=existing.payer_id, normalised=normalised, created=False, conflicted=True
            )
        return AliasOutcome(payer_id=payer_id, normalised=normalised, created=False)

    session.add(PayerAlias(payer_id=payer_id, alias=name.strip(), normalised_alias=normalised))
    session.flush()
    return AliasOutcome(payer_id=payer_id, normalised=normalised, created=True)


def resolve_by_name(session: Session, name: str) -> str | None:
    """The `payer_id` a name resolves to, or `None`.

    **Confirmed aliases only.** An unconfirmed alias is a record that a
    spelling was seen, which is not the same as a decision that the payer
    should be found by it -- and resolving through one is how two payers
    quietly become one.

    Returns `None` rather than a best guess. PD-18: on uncertainty the system
    does not act.
    """
    normalised = normalise_payer_name(name)
    if not normalised:
        return None
    return session.scalar(
        select(PayerAlias.payer_id).where(
            PayerAlias.normalised_alias == normalised,
            PayerAlias.confirmed.is_(True),
        )
    )


def propose_matches(session: Session) -> int:
    """Scan the payer registry for identifiers that look like one payer.

    Compares every payer against every other, which is quadratic and fine:
    this runs over a registry of payers, not of claims, and a registry large
    enough for that to matter is one where the review queue would already have
    collapsed under its own weight.

    Returns the number of new candidates raised. Existing candidates are left
    alone, including rejected ones -- re-proposing something a human has
    already turned down is how a review queue becomes noise.
    """
    payers = session.scalars(select(Payer).order_by(Payer.payer_id)).all()
    raised = 0

    for index, left in enumerate(payers):
        for right in payers[index + 1 :]:
            score = similarity(left.name, right.name)
            if score < REVIEW_THRESHOLD:
                continue
            if _propose(
                session,
                left.payer_id,
                right.payer_id,
                matched_on=left.name,
                confidence=Decimal(str(round(score, 3))),
            ):
                raised += 1

    if raised:
        log.info("payer.match_candidates_raised", count=raised)
    return raised


def pending_matches(session: Session) -> tuple[PayerMatchCandidate, ...]:
    """The review queue: candidates nobody has decided on yet."""
    return tuple(
        session.scalars(
            select(PayerMatchCandidate)
            .where(PayerMatchCandidate.status == str(MatchCandidateStatus.PENDING))
            .order_by(PayerMatchCandidate.confidence.desc(), PayerMatchCandidate.created_at)
        ).all()
    )


def review_match(
    session: Session,
    candidate: PayerMatchCandidate,
    *,
    confirmed: bool,
    reviewed_by: str,
) -> None:
    """Record a human's decision on a candidate.

    Confirming does not merge anything. It records that a person looked and
    said yes, which is what a later migration of the two payers would need as
    its justification -- BR-STATE-01's "logged with timestamp, actor and
    reason" applied to identity rather than to state.
    """
    candidate.status = str(
        MatchCandidateStatus.CONFIRMED if confirmed else MatchCandidateStatus.REJECTED
    )
    candidate.reviewed_at = datetime.now(UTC)
    candidate.reviewed_by = reviewed_by


def confirm_alias(session: Session, *, normalised_alias: str, reviewed_by: str) -> bool:
    """Mark one spelling as a name this payer may be resolved by.

    Separate from `record_alias` on purpose: observing a spelling and trusting
    it are different acts, and only the second one lets a name decide where
    money is attributed.
    """
    alias = session.scalar(
        select(PayerAlias).where(PayerAlias.normalised_alias == normalised_alias)
    )
    if alias is None:
        return False
    alias.confirmed = True
    log.info("payer.alias_confirmed", normalised_alias=normalised_alias, actor=reviewed_by)
    return True


def _propose(
    session: Session,
    first: str,
    second: str,
    *,
    matched_on: str,
    confidence: Decimal,
) -> bool:
    """Raise a candidate if this pair has not been raised before.

    The pair is ordered before storing, so that proposing (A, B) and (B, A)
    are recognised as the same proposal rather than two.
    """
    low, high = sorted((first, second))
    if low == high:
        return False

    already = session.scalar(
        select(PayerMatchCandidate).where(
            PayerMatchCandidate.payer_id == low,
            PayerMatchCandidate.other_payer_id == high,
        )
    )
    if already is not None:
        return False

    session.add(
        PayerMatchCandidate(
            payer_id=low,
            other_payer_id=high,
            matched_on=matched_on[:255],
            confidence=confidence,
        )
    )
    session.flush()
    return True
