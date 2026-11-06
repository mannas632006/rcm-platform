"""What the INSIGHT and ADMIN screens read (M5.9-M5.11, pulled forward).

Every number here is read from CORE and the work queue -- never mocked -- and
every client-owned count is summed over the principal's client scope only
(PD-20). The payer registry and the rules are platform reference data, not a
client's, so they are not client-filtered; their routes still require a
permission.

Nothing here returns a patient identifier: counts, rates, state names, rule
ids and payer ids only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from rcm.analytics.denials import denial_rate, denied_submitter_claim_ids
from rcm.decision.registry import is_stale
from rcm.decision.registry_models import PayerRegistryEntry
from rcm.decision.rules.models import RuleBacktest, RuleVersion
from rcm.domain.acknowledgement import (
    AcknowledgementOutcome,
    AcknowledgementSource,
    ClaimAcknowledgement,
)
from rcm.domain.claim import Claim
from rcm.domain.claim_state import ClaimState
from rcm.domain.claim_state_log import ClaimStateTransition, ReviewStatus
from rcm.domain.payer import Payer
from rcm.domain.remittance import ClaimPayment
from rcm.platform.auth import Principal
from rcm.platform.config import get_settings
from rcm.work import views


@dataclass(frozen=True, slots=True)
class Dashboard:
    adjudicated_claims: int
    denied_claims: int
    #: Denied over adjudicated, per claim (BR-DATA-03). None with no data.
    denial_rate: Decimal | None
    #: First submissions the payer took without a rejection or a denial.
    first_pass_claims: int
    first_pass_clean: int
    first_pass_clean_rate: Decimal | None
    claims_by_state: dict[str, int]
    open_work: int
    pinned_work: int
    overdue_work: int
    steps_awaiting_review: int


def _rate(numerator: int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator) * 100).quantize(Decimal("0.01"))


def dashboard(session: Session, *, principal: Principal, now: datetime) -> Dashboard:
    clients = sorted(principal.client_ids)

    adjudicated = denied = 0
    first_pass = first_clean = 0
    for client_id in clients:
        rate = denial_rate(session, client_id=client_id)
        adjudicated += rate.claims
        denied += rate.denied_claims
        total, clean = _first_pass(session, client_id=client_id)
        first_pass += total
        first_clean += clean

    # PD-7: one claim per chain, not one per submission row. The chain's state
    # is its live submission's (ADR-0006): the earliest not yet Replaced -- the
    # original until the payer takes the replacement, the replacement after.
    live = (
        select(Claim.root_claim_id, Claim.state)
        .where(Claim.client_id.in_(clients), Claim.state != str(ClaimState.REPLACED))
        .ext(distinct_on(Claim.root_claim_id))
        .order_by(Claim.root_claim_id, Claim.submission_attempt)
        .subquery()
    )
    by_state: dict[str, int] = dict(
        session.execute(
            select(live.c.state, func.count()).group_by(live.c.state).order_by(live.c.state)
        ).all()
    )
    queues = views.queue_summaries(session, principal=principal, now=now)
    awaiting = session.scalar(
        select(func.count()).where(
            ClaimStateTransition.client_id.in_(clients),
            ClaimStateTransition.review_status == str(ReviewStatus.PENDING),
        )
    )

    return Dashboard(
        adjudicated_claims=adjudicated,
        denied_claims=denied,
        denial_rate=_rate(denied, adjudicated),
        first_pass_claims=first_pass,
        first_pass_clean=first_clean,
        first_pass_clean_rate=_rate(first_clean, first_pass),
        claims_by_state=by_state,
        open_work=sum(q.open for q in queues),
        pinned_work=sum(q.pinned for q in queues),
        overdue_work=sum(q.overdue for q in queues),
        steps_awaiting_review=awaiting or 0,
    )


def _first_pass(session: Session, *, client_id: uuid.UUID) -> tuple[int, int]:
    """First submissions with a payer verdict, and how many were clean.

    A first submission (attempt 1) counts once the payer has said something
    -- a 277CA verdict or an 835. It is clean when the 277CA did not reject
    it and the 835 did not deny it: the claim went through on the first try.
    """
    rejected = select(ClaimAcknowledgement.claim_id).where(
        ClaimAcknowledgement.client_id == client_id,
        ClaimAcknowledgement.source == str(AcknowledgementSource.CLAIM_277CA),
        ClaimAcknowledgement.outcome == str(AcknowledgementOutcome.REJECTED),
    )
    verdict = select(ClaimAcknowledgement.claim_id).where(
        ClaimAcknowledgement.client_id == client_id,
        ClaimAcknowledgement.source == str(AcknowledgementSource.CLAIM_277CA),
    )
    remitted = select(ClaimPayment.claim_id).where(ClaimPayment.client_id == client_id)
    denied = denied_submitter_claim_ids(client_id)

    base = and_(
        Claim.client_id == client_id,
        Claim.submission_attempt == 1,
        Claim.submitter_claim_id.in_(verdict) | Claim.submitter_claim_id.in_(remitted),
    )
    total = session.scalar(select(func.count()).where(base)) or 0
    dirty = (
        session.scalar(
            select(func.count()).where(
                base,
                Claim.submitter_claim_id.in_(rejected) | Claim.submitter_claim_id.in_(denied),
            )
        )
        or 0
    )
    return total, total - dirty


@dataclass(frozen=True, slots=True)
class RuleRow:
    rule_id: str
    version: int
    layer: str
    severity: str
    status: str
    created_at: datetime
    backtest_flagged: int | None
    backtest_fp_rate: Decimal | None


def rules(session: Session, *, principal: Principal) -> list[RuleRow]:
    """The latest version of each rule, with its latest backtest inside the
    principal's client scope (a backtest is a client's history)."""
    latest = (
        select(RuleVersion.rule_id, func.max(RuleVersion.version).label("version"))
        .group_by(RuleVersion.rule_id)
        .subquery()
    )
    versions = session.scalars(
        select(RuleVersion)
        .join(
            latest,
            and_(RuleVersion.rule_id == latest.c.rule_id, RuleVersion.version == latest.c.version),
        )
        .order_by(RuleVersion.rule_id)
    ).all()

    out: list[RuleRow] = []
    for rule in versions:
        backtest = session.scalar(
            select(RuleBacktest)
            .where(
                RuleBacktest.rule_id == rule.rule_id,
                RuleBacktest.version == rule.version,
                RuleBacktest.client_id.in_(principal.client_ids),
            )
            .order_by(RuleBacktest.ran_at.desc())
            .limit(1)
        )
        out.append(
            RuleRow(
                rule_id=rule.rule_id,
                version=rule.version,
                layer=rule.layer,
                severity=rule.severity,
                status=rule.status,
                created_at=rule.created_at,
                backtest_flagged=backtest.flagged if backtest else None,
                backtest_fp_rate=backtest.fp_rate if backtest else None,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class RegistryRow:
    payer_id: str
    payer_name: str
    plan_type: str
    cpt: str
    version: int
    prior_auth_required: bool | None
    last_verified_date: date
    stale: bool


def payer_registry(session: Session, *, today: date) -> list[RegistryRow]:
    """The current version of each registry entry, flagged when stale
    (BR-REG-02: a stale entry cannot drive a BLOCKING decision)."""
    days = get_settings().payer_registry_staleness_days
    latest = (
        select(
            PayerRegistryEntry.payer_id,
            PayerRegistryEntry.plan_type,
            PayerRegistryEntry.cpt,
            func.max(PayerRegistryEntry.version).label("version"),
        )
        .group_by(PayerRegistryEntry.payer_id, PayerRegistryEntry.plan_type, PayerRegistryEntry.cpt)
        .subquery()
    )
    rows = session.execute(
        select(PayerRegistryEntry, Payer.name)
        .join(
            latest,
            and_(
                PayerRegistryEntry.payer_id == latest.c.payer_id,
                PayerRegistryEntry.plan_type == latest.c.plan_type,
                PayerRegistryEntry.cpt == latest.c.cpt,
                PayerRegistryEntry.version == latest.c.version,
            ),
        )
        .join(Payer, Payer.payer_id == PayerRegistryEntry.payer_id)
        .order_by(PayerRegistryEntry.payer_id, PayerRegistryEntry.cpt)
    ).all()
    return [
        RegistryRow(
            payer_id=entry.payer_id,
            payer_name=name,
            plan_type=entry.plan_type,
            cpt=entry.cpt,
            version=entry.version,
            prior_auth_required=entry.prior_auth_required,
            last_verified_date=entry.last_verified_date,
            stale=is_stale(entry.last_verified_date, today=today, staleness_days=days),
        )
        for entry, name in rows
    ]
