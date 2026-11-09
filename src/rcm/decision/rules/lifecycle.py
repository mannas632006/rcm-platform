"""Rule lifecycle: the only way a rule's status changes (M4.5, M4.6, ADR-0010).

FR-RULES-06: "Rules are promoted through: draft -> backtest -> shadow ->
advisory -> blocking. No shortcuts." The steps are declared below as data, as
the claim state machine's are.

Two gates on evidence:

- into SHADOW: a backtest of this exact rule version is recorded (BR-M7-04);
- into BLOCKING: the rule's severity allows it, every client it was
  backtested on has a measured false-positive rate, and the worst of them is
  within [TBD-45] -- the owner's 10% (FR-RULES-03).

Every change is kept, with who made it, why, and the backtest it rested on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.decision.rules.definition import LIVE_STATUSES, RuleStatus, Severity, parse_rule
from rcm.decision.rules.models import RuleBacktest, RuleStatusChange, RuleVersion
from rcm.platform.config import get_settings
from rcm.platform.errors import (
    ResourceNotFoundError,
    StateTransitionError,
    ValidationFailedError,
)
from rcm.platform.logging import get_logger

log = get_logger(__name__)

S = RuleStatus

#: SRS 8.3. A step not listed here does not exist.
TRANSITIONS: Final[dict[RuleStatus, frozenset[RuleStatus]]] = {
    S.DRAFT: frozenset({S.BACKTESTING}),
    S.BACKTESTING: frozenset({S.DRAFT, S.SHADOW}),
    S.SHADOW: frozenset({S.ADVISORY}),
    S.ADVISORY: frozenset({S.BLOCKING, S.DRAFT}),
    S.BLOCKING: frozenset({S.DEPRECATED, S.DRAFT}),
    S.DEPRECATED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StatusChange:
    rule_id: str
    version: int
    from_status: RuleStatus
    to_status: RuleStatus
    #: The backtest the step rested on, for SHADOW and BLOCKING.
    backtest_id: uuid.UUID | None
    fp_rate: Decimal | None


def _refuse(source: RuleStatus, target: RuleStatus, reason: str) -> StateTransitionError:
    return StateTransitionError(source=str(source), target=str(target), reason=reason)


def _latest_per_client(session: Session, rule_id: str, version: int) -> list[RuleBacktest]:
    """Each client's most recent backtest of this version that saw a claim."""
    newest = (
        select(RuleBacktest.client_id, func.max(RuleBacktest.ran_at).label("ran_at"))
        .where(
            RuleBacktest.rule_id == rule_id,
            RuleBacktest.version == version,
            RuleBacktest.claims_evaluated > 0,
        )
        .group_by(RuleBacktest.client_id)
        .subquery()
    )
    return list(
        session.scalars(
            select(RuleBacktest)
            .join(
                newest,
                (RuleBacktest.client_id == newest.c.client_id)
                & (RuleBacktest.ran_at == newest.c.ran_at),
            )
            .where(RuleBacktest.rule_id == rule_id, RuleBacktest.version == version)
            .order_by(RuleBacktest.client_id)
        ).all()
    )


def _evidence_for_shadow(session: Session, row: RuleVersion, source: RuleStatus) -> RuleBacktest:
    backtests = _latest_per_client(session, row.rule_id, row.version)
    if not backtests:
        raise _refuse(source, S.SHADOW, "no backtest of this rule version is recorded")
    return max(backtests, key=lambda b: b.ran_at)


def _evidence_for_blocking(session: Session, row: RuleVersion, source: RuleStatus) -> RuleBacktest:
    if parse_rule(row.document).severity is not Severity.BLOCKING:
        raise _refuse(
            source, S.BLOCKING, f"the rule's severity is {row.severity}, which never blocks"
        )
    backtests = _latest_per_client(session, row.rule_id, row.version)
    if not backtests:
        raise _refuse(source, S.BLOCKING, "no backtest of this rule version is recorded")
    measured: list[tuple[Decimal, RuleBacktest]] = []
    for backtest in backtests:
        if backtest.fp_rate is None:
            # ADR-0009: a rule never seen stopping a claim has shown nothing
            # about its accuracy.
            raise _refuse(
                source,
                S.BLOCKING,
                "a backtest flagged no claim, so the false-positive rate is unmeasured",
            )
        measured.append((backtest.fp_rate, backtest))
    cap = get_settings().blocking_rule_max_false_positive_rate
    worst_rate, worst = max(measured, key=lambda pair: pair[0])
    if worst_rate > cap:
        raise _refuse(
            source,
            S.BLOCKING,
            f"false-positive rate {worst_rate} is above the {cap} cap (FR-RULES-03); "
            "it may stay ADVISORY",
        )
    return worst


def _check(
    session: Session, row: RuleVersion, source: RuleStatus, to: RuleStatus
) -> RuleBacktest | None:
    """Every condition on the step `source` -> `to`, refused by name; the
    backtest it rests on, if it needs one. Writes nothing, so `promotion`
    can ask the same question `change_status` answers (UR-30)."""
    if to not in TRANSITIONS[source]:
        raise _refuse(source, to, "not a step in the rule lifecycle (FR-RULES-06)")

    evidence: RuleBacktest | None = None
    if to is S.SHADOW:
        evidence = _evidence_for_shadow(session, row, source)
    elif to is S.BLOCKING:
        evidence = _evidence_for_blocking(session, row, source)

    if to in LIVE_STATUSES and source not in LIVE_STATUSES:
        other = session.scalar(
            select(RuleVersion.version).where(
                RuleVersion.rule_id == row.rule_id,
                RuleVersion.version != row.version,
                RuleVersion.status.in_([str(s) for s in LIVE_STATUSES]),
            )
        )
        if other is not None:
            raise _refuse(
                source, to, f"version {other} of this rule is live; withdraw or deprecate it first"
            )
    return evidence


#: The step Promote takes from each status (S-06). Withdrawing to DRAFT and
#: deprecating are steps too, but not promotions; BLOCKING is the end.
PROMOTION: Final[dict[RuleStatus, RuleStatus]] = {
    S.DRAFT: S.BACKTESTING,
    S.BACKTESTING: S.SHADOW,
    S.SHADOW: S.ADVISORY,
    S.ADVISORY: S.BLOCKING,
}


@dataclass(frozen=True, slots=True)
class Promotion:
    #: The next status, or None when the version has no promotion left.
    to: RuleStatus | None
    #: Why `change_status` would refuse the step now; None when it would not.
    blocked_by: str | None


def promotion(session: Session, *, row: RuleVersion) -> Promotion:
    """UR-30: where Promote would take this version, and why it cannot yet,
    by the same checks `change_status` makes -- so the screen never offers a
    step the API refuses, or hides one it would take."""
    source = RuleStatus(row.status)
    to = PROMOTION.get(source)
    if to is None:
        return Promotion(to=None, blocked_by=None)
    try:
        _check(session, row, source, to)
    except StateTransitionError as refused:
        return Promotion(to=to, blocked_by=str(refused.context["reason"]))
    return Promotion(to=to, blocked_by=None)


def change_status(
    session: Session,
    *,
    rule_id: str,
    version: int,
    to: RuleStatus,
    changed_by: str,
    reason: str,
) -> StatusChange:
    """Move one rule version one step along the lifecycle, or refuse by name."""
    if not changed_by.strip():
        raise ValidationFailedError(field="changed_by", reason="name who made the change")
    if not reason.strip():
        raise ValidationFailedError(field="reason", reason="say why")
    row = session.scalar(
        select(RuleVersion)
        .where(RuleVersion.rule_id == rule_id, RuleVersion.version == version)
        .with_for_update()
    )
    if row is None:
        raise ResourceNotFoundError(resource="rule_version")
    source = RuleStatus(row.status)
    evidence = _check(session, row, source, to)

    row.status = str(to)
    change = RuleStatusChange(
        rule_id=rule_id,
        version=version,
        from_status=str(source),
        to_status=str(to),
        changed_by=changed_by,
        reason=reason,
        backtest_id=evidence.backtest_id if evidence is not None else None,
        fp_rate=evidence.fp_rate if evidence is not None else None,
    )
    session.add(change)
    session.flush()
    log.info(
        "rule.status_changed",
        rule_id=rule_id,
        version=version,
        source=str(source),
        target=str(to),
    )
    return StatusChange(
        rule_id=rule_id,
        version=version,
        from_status=source,
        to_status=to,
        backtest_id=change.backtest_id,
        fp_rate=change.fp_rate,
    )
