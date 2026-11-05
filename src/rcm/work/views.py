"""Read views over work, for the operator UI (M5.1, SRS 10.2, 10.4.0).

The application shell needs two answers on every screen: how much work is in
each queue the operator may see, and how many claims are about to miss their
filing deadline (UR-26). Both are read here, through the query-layer client
scope (PD-20), so a route cannot ask for them unscoped.

The queues of SRS 10.2 are groupings of work item kinds, declared as data.
Every kind belongs to exactly one queue -- a kind in none would be work no
screen shows, which a test forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim
from rcm.platform.auth import Permission, Principal, scoped_query
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import OPEN_STATES

K = WorkItemKind
_CENT: Final = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class QueueDef:
    key: str
    label: str
    kinds: frozenset[WorkItemKind]
    #: Who may see it. A queue is listed only to a principal holding this.
    permission: Permission


#: SRS 10.2's queues, in the sidebar's order.
QUEUES: Final[tuple[QueueDef, ...]] = (
    QueueDef(
        "ar-follow-up",
        "AR Follow-up",
        frozenset(
            {
                K.SUBMISSION_REJECTION,
                K.DEADLINE_RISK,
                K.MISSING_FILING_DEADLINE,
                K.CLAIM_STEP_REVIEW,
            }
        ),
        Permission.CLAIM_READ,
    ),
    QueueDef("denials", "Denial Workbench", frozenset({K.DENIAL}), Permission.DENIAL_READ),
    QueueDef(
        "scrubber", "Scrubber Exceptions", frozenset({K.SCRUB_EXCEPTION}), Permission.CLAIM_READ
    ),
    QueueDef(
        "payments", "Payment Exceptions", frozenset({K.PAYMENT_EXCEPTION}), Permission.CLAIM_READ
    ),
    QueueDef(
        "eligibility",
        "Eligibility Exceptions",
        frozenset({K.ELIGIBILITY_PROBLEM}),
        Permission.CLAIM_READ,
    ),
    QueueDef("prior-auth", "Prior Auth", frozenset({K.AUTH_PROBLEM}), Permission.CLAIM_READ),
    QueueDef(
        "data-quality",
        "Data Quality",
        frozenset({K.DATA_QUALITY, K.PAYER_MATCH_REVIEW}),
        Permission.CLAIM_READ,
    ),
    QueueDef(
        "rules-review",
        "Rules Review",
        frozenset({K.RULE_CONFLICT, K.MODEL_LOW_CONFIDENCE}),
        Permission.RULE_READ,
    ),
    QueueDef(
        "operations",
        "Operations",
        frozenset({K.COMPLIANCE_CONCERN, K.EXTERNAL_SYSTEM_FAILURE}),
        Permission.REPORT_READ,
    ),
)


@dataclass(frozen=True, slots=True)
class QueueSummary:
    key: str
    label: str
    open: int
    unassigned: int
    #: Held by the asking operator.
    mine: int
    #: Deadline-pinned (PD-15): shown first, whatever the filter.
    pinned: int
    #: Past their SLA (BR-EXC-02).
    overdue: int


def visible_queues(principal: Principal) -> tuple[QueueDef, ...]:
    return tuple(q for q in QUEUES if principal.has(q.permission))


def queue_for(kind: WorkItemKind) -> QueueDef:
    """The one queue a kind belongs to."""
    for queue in QUEUES:
        if kind in queue.kinds:
            return queue
    raise LookupError(f"{kind} is in no queue")


def queue_summaries(session: Session, *, principal: Principal, now: datetime) -> list[QueueSummary]:
    """Open work per queue the principal may see, inside its client scope."""
    queues = visible_queues(principal)
    if not queues:
        return []
    counts: dict[str, tuple[int, int, int, int, int]] = {
        kind: (total, unassigned, mine, pinned, overdue)
        for kind, total, unassigned, mine, pinned, overdue in session.execute(
            scoped_query(
                select(
                    WorkItem.kind,
                    func.count(),
                    func.count().filter(WorkItem.assigned_to.is_(None)),
                    func.count().filter(WorkItem.assigned_to == principal.subject),
                    func.count().filter(WorkItem.deadline_pinned.is_(True)),
                    func.count().filter(WorkItem.sla_due_at < now),
                ).where(WorkItem.state.in_([str(s) for s in OPEN_STATES])),
                WorkItem,
                principal,
            ).group_by(WorkItem.kind)
        )
    }
    summaries = []
    for queue in queues:
        totals = [0, 0, 0, 0, 0]
        for kind in queue.kinds:
            for i, value in enumerate(counts.get(str(kind), (0, 0, 0, 0, 0))):
                totals[i] += value
        open_, unassigned, mine, pinned, overdue = totals
        summaries.append(
            QueueSummary(queue.key, queue.label, open_, unassigned, mine, pinned, overdue)
        )
    return summaries


@dataclass(frozen=True, slots=True)
class DeadlineRisk:
    """UR-26's alert bar: claims whose filing deadline pinned their work."""

    claims: int
    #: Billed charges of those claims: what is lost if they are not filed.
    charges: Decimal


def deadline_risk(session: Session, *, principal: Principal) -> DeadlineRisk:
    pinned_claims = scoped_query(
        select(WorkItem.claim_id)
        .where(
            WorkItem.deadline_pinned.is_(True),
            WorkItem.state.in_([str(s) for s in OPEN_STATES]),
            WorkItem.claim_id.is_not(None),
        )
        .distinct(),
        WorkItem,
        principal,
    )
    claims, charges = session.execute(
        scoped_query(
            select(func.count(), func.coalesce(func.sum(Claim.total_charge), 0)).where(
                Claim.claim_id.in_(pinned_claims)
            ),
            Claim,
            principal,
        )
    ).one()
    # To the cent always, so the API's string is "0.00", never "0".
    return DeadlineRisk(claims=int(claims), charges=Decimal(charges).quantize(_CENT))
