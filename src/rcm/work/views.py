"""Read views over work, for the operator UI (M5.1, M5.3, SRS 10.2, 10.4).

The application shell needs two answers on every screen: how much work is in
each queue the operator may see, and how many claims are about to miss their
filing deadline (UR-26). Both are read here, through the query-layer client
scope (PD-20), so a route cannot ask for them unscoped.

The queues of SRS 10.2 are groupings of work item kinds, declared as data.
Every kind belongs to exactly one queue -- a kind in none would be work no
screen shows, which a test forbids.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, cast

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session

from rcm.decision import scoring
from rcm.domain.claim import Claim
from rcm.domain.payer import Payer
from rcm.platform.auth import Permission, Principal, scoped_query
from rcm.platform.errors import ResourceNotFoundError, ValidationFailedError
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.priority import queue_order
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


# ---------------------------------------------------------------------------
# A queue's items (M5.3, S-02)
# ---------------------------------------------------------------------------


class Assignment(StrEnum):
    ANY = "any"
    MINE = "mine"
    UNASSIGNED = "unassigned"


@dataclass(frozen=True, slots=True)
class QueueItem:
    work_item_id: uuid.UUID
    kind: str
    title: str
    state: str
    priority_score: int
    deadline_pinned: bool
    days_to_deadline: int | None
    assigned_to: str | None
    sla_due_at: datetime
    overdue: bool
    bulk_handling: bool
    created_at: datetime
    claim_id: uuid.UUID | None
    submitter_claim_id: str | None
    payer_id: str | None
    payer_name: str | None
    date_of_service: date | None
    charges: Decimal | None
    #: ML-3. None when the model gave none: shown as unavailable, never 0.
    recovery_probability: float | None = None


@dataclass(frozen=True, slots=True)
class QueuePage:
    items: list[QueueItem]
    #: Opaque; pass back for the next page. None on the last page.
    next_cursor: str | None
    #: UR-34: why this page carries no model scores, or None if it does.
    recovery_unavailable: str | None = None


MAX_PAGE: Final = 500


def _encode(keys: Sequence[object]) -> str:
    plain = [k.isoformat() if isinstance(k, datetime) else str(k) for k in keys]
    return base64.urlsafe_b64encode(json.dumps(plain).encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[object, ...]:
    """The last row's sort keys. A cursor that is not ours is refused by
    name, never read as "from the start" (PD-18)."""
    refused = ValidationFailedError(field="cursor", reason="not a cursor this queue issued")
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        # `_encode` writes seven strings and nothing else. Anything else --
        # a number, Infinity, a nested object -- was not issued here.
        if not isinstance(raw, list) or len(raw) != 7 or not all(isinstance(k, str) for k in raw):
            raise refused
        pinned, deadline, unscored, score, sla, created, work_item_id = raw
        sla_at, created_at = datetime.fromisoformat(sla), datetime.fromisoformat(created)
        if sla_at.tzinfo is None or created_at.tzinfo is None:
            # Ours always carry an offset; a naive time would be read in the
            # session's zone and quietly shift the page.
            raise refused
        return (
            int(pinned),
            int(deadline),
            int(unscored),
            int(score),
            sla_at,
            created_at,
            uuid.UUID(work_item_id),
        )
    except (ValueError, binascii.Error):
        raise refused from None


def _item(row: Sequence[object], now: datetime, recovery: Mapping[uuid.UUID, float]) -> QueueItem:
    """One result row: the item, its claim's columns, then the sort keys."""
    item = cast("WorkItem", row[0])
    charge = cast("Decimal | None", row[5])
    return QueueItem(
        work_item_id=item.work_item_id,
        kind=item.kind,
        title=item.title,
        state=item.state,
        priority_score=item.priority_score,
        deadline_pinned=item.deadline_pinned,
        days_to_deadline=item.days_to_deadline,
        assigned_to=item.assigned_to,
        sla_due_at=item.sla_due_at,
        overdue=item.sla_due_at < now,
        bulk_handling=item.bulk_handling,
        created_at=item.created_at,
        claim_id=item.claim_id,
        submitter_claim_id=cast("str | None", row[1]),
        payer_id=cast("str | None", row[2]),
        payer_name=cast("str | None", row[3]),
        date_of_service=cast("date | None", row[4]),
        charges=charge.quantize(_CENT) if charge is not None else None,
        recovery_probability=recovery.get(item.claim_id) if item.claim_id else None,
    )


def queue_items(
    session: Session,
    *,
    principal: Principal,
    key: str,
    now: datetime,
    cursor: str | None = None,
    limit: int = 200,
    assignment: Assignment = Assignment.ANY,
    payer_id: str | None = None,
) -> QueuePage:
    """One page of a queue, best first (UR-02), inside the client scope.

    Keyset-paginated on the queue's own order (FR-API-02): a page never
    repeats or skips an item because others were resolved meanwhile. An item
    re-scored between pages (`reprioritise_open`) moves to its new place and
    can be seen twice or not at all on that walk; the next poll shows it.
    Deadline-pinned items are returned whatever the filter (UR-03).
    """
    queue = next((q for q in visible_queues(principal) if q.key == key), None)
    if queue is None:
        # Not a queue, or not one this role may see: the same answer for both.
        raise ResourceNotFoundError(resource="work_queue")
    if not 1 <= limit <= MAX_PAGE:
        raise ValidationFailedError(field="limit", reason=f"between 1 and {MAX_PAGE}")

    order = queue_order()
    statement = (
        select(
            WorkItem,
            Claim.submitter_claim_id,
            Claim.payer_id,
            Payer.name,
            Claim.date_of_service,
            Claim.total_charge,
            # Labelled: several keys are the item's own columns, and an
            # unlabelled repeat is an ambiguous name in the result.
            *(key.label(f"sort_{n}") for n, key in enumerate(order)),
        )
        .outerjoin(
            Claim,
            and_(Claim.claim_id == WorkItem.claim_id, Claim.client_id == WorkItem.client_id),
        )
        .outerjoin(Payer, Payer.payer_id == Claim.payer_id)
        .where(
            WorkItem.kind.in_([str(k) for k in queue.kinds]),
            WorkItem.state.in_([str(s) for s in OPEN_STATES]),
        )
    )
    narrowing = []
    if assignment is Assignment.MINE:
        narrowing.append(WorkItem.assigned_to == principal.subject)
    elif assignment is Assignment.UNASSIGNED:
        narrowing.append(WorkItem.assigned_to.is_(None))
    if payer_id is not None:
        narrowing.append(Claim.payer_id == payer_id)
    if narrowing:
        # UR-03: a filter narrows ordinary work; it never hides pinned work.
        statement = statement.where(or_(WorkItem.deadline_pinned.is_(True), and_(*narrowing)))
    if cursor is not None:
        statement = statement.where(tuple_(*order) > tuple_(*_decode(cursor)))

    rows = session.execute(
        scoped_query(statement, WorkItem, principal).order_by(*order).limit(limit + 1)
    ).all()
    page = rows[:limit]
    recovery, unavailable = _recovery(session, page)
    items = [_item(row, now, recovery) for row in page]
    next_cursor = _encode(tuple(rows[limit - 1])[6:]) if len(rows) > limit else None
    return QueuePage(items=items, next_cursor=next_cursor, recovery_unavailable=unavailable)


def _recovery(
    session: Session, page: Sequence[Sequence[object]]
) -> tuple[dict[uuid.UUID, float], str | None]:
    """UR-34: the page's model scores, or why there are none. The queue is
    ordered and worked on rules alone, so a missing score costs a column,
    never the page."""
    claims: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for row in page:
        item = cast("WorkItem", row[0])
        if item.claim_id is not None:
            claims[item.client_id].append(item.claim_id)
    values: dict[uuid.UUID, float] = {}
    unavailable = None if scoring.deployed() else scoring.NOT_DEPLOYED
    for client_id, claim_ids in claims.items():
        scores = scoring.recovery_scores(session, client_id=client_id, claim_ids=claim_ids)
        unavailable = scores.unavailable or unavailable
        values.update(scores.values)
    return values, unavailable
