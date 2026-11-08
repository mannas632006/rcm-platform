"""Take, give back and resolve one item from a queue (M5.4, ADR-0021).

SRS 10.9's queue keys `a` (assign to me) and `r` (resolve) need these;
`rcm.work.queue` holds the locking rules, and this module adds the two
things a person needs before reaching them:

- **Scope and role.** The item is read through the principal's client scope
  (PD-20), and the principal must be able to see its queue *and* hold the
  queue's work permission. An item outside either reads as not found, the
  same answer for both.
- **What a plain resolve may say.** Only outcomes that change nothing
  elsewhere: `NO_ACTION_NEEDED` and `DUPLICATE`, each with a note saying
  why (FR-WORK-04). A denial is never resolved here: writing it off,
  appealing it or correcting it has consequences the Denial Workbench
  enforces (BR-M11-05, UR-09, PD-7), so a plain resolve is refused by name.

Resolving does not lift an automation suspension: that stays its own,
reviewed step (BR-EXC-03, `exceptions.lift_suspension`). An escalated item
is a supervisor's: it is neither taken nor resolved from the queue.

The row is locked when it is first read, so every decision below -- who
holds it, whether the lease has lapsed, what state it is in -- is made on
the row as it is, not as it was a moment ago.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.platform.auth import Principal, scoped_query
from rcm.platform.errors import (
    ResourceNotFoundError,
    StateTransitionError,
    ValidationFailedError,
)
from rcm.work import queue, views
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import WorkItemOutcome, WorkItemState

#: What a resolve from the queue may record. Every other outcome belongs to
#: a workflow that does the thing the outcome names.
PLAIN_OUTCOMES: Final = frozenset({WorkItemOutcome.NO_ACTION_NEEDED, WorkItemOutcome.DUPLICATE})


@dataclass(frozen=True, slots=True)
class Holding:
    work_item_id: uuid.UUID
    state: str
    assigned_to: str | None
    lock_expires_at: datetime | None
    outcome: str | None


def _held(item: WorkItem) -> Holding:
    return Holding(
        work_item_id=item.work_item_id,
        state=item.state,
        assigned_to=item.assigned_to,
        lock_expires_at=item.lock_expires_at,
        outcome=item.outcome,
    )


def _workable(session: Session, *, principal: Principal, work_item_id: uuid.UUID) -> WorkItem:
    """The item, locked, if this principal may work it; not found otherwise
    (PD-20). An escalated item is refused by name."""
    item = session.scalar(
        scoped_query(
            select(WorkItem).where(WorkItem.work_item_id == work_item_id), WorkItem, principal
        ).with_for_update()
    )
    if item is not None:
        try:
            home = views.queue_for(WorkItemKind(item.kind))
        except (LookupError, ValueError):
            home = None
        if home and principal.has(home.permission) and principal.has(home.work):
            if item.state == str(WorkItemState.ESCALATED):
                raise StateTransitionError(
                    source=item.state,
                    target=str(WorkItemState.ASSIGNED),
                    reason="an escalated item waits for a supervisor",
                )
            return item
    raise ResourceNotFoundError(entity="work_item", entity_id=str(work_item_id))


def _mine(item: WorkItem, principal: Principal, now: datetime) -> bool:
    """Held by this principal, on a lease that has not lapsed."""
    lapsed = item.lock_expires_at is not None and item.lock_expires_at <= now
    return item.assigned_to == principal.subject and not lapsed


def take(
    session: Session, *, principal: Principal, work_item_id: uuid.UUID, now: datetime
) -> Holding:
    """Assign the item to the caller (FR-WORK-01). Taking an item one
    already holds renews the lease rather than failing; one whose lease has
    lapsed is taken afresh."""
    item = _workable(session, principal=principal, work_item_id=work_item_id)
    if _mine(item, principal, now):
        queue.renew(
            session,
            work_item_id=work_item_id,
            client_id=item.client_id,
            operator=principal.subject,
            now=now,
        )
    else:
        queue.claim(
            session,
            work_item_id=work_item_id,
            client_id=item.client_id,
            operator=principal.subject,
            now=now,
        )
    session.refresh(item)
    return _held(item)


def give_back(
    session: Session, *, principal: Principal, work_item_id: uuid.UUID, now: datetime
) -> Holding:
    """Return the item to the queue. Only its holder may (FR-WORK-01)."""
    item = _workable(session, principal=principal, work_item_id=work_item_id)
    queue.release(
        session,
        work_item_id=work_item_id,
        client_id=item.client_id,
        operator=principal.subject,
        now=now,
    )
    session.refresh(item)
    return _held(item)


def resolve(
    session: Session,
    *,
    principal: Principal,
    work_item_id: uuid.UUID,
    outcome: WorkItemOutcome,
    note: str,
    now: datetime,
) -> Holding:
    """Close the item with a structured outcome and the reason for it
    (FR-WORK-04), taking it first if nobody holds it."""
    item = _workable(session, principal=principal, work_item_id=work_item_id)
    if item.kind == str(WorkItemKind.DENIAL):
        raise ValidationFailedError(
            field="work_item",
            reason="a denial is resolved by its disposition in the Denial Workbench",
        )
    if outcome not in PLAIN_OUTCOMES:
        raise ValidationFailedError(
            field="outcome",
            reason="only NO_ACTION_NEEDED or DUPLICATE may be recorded from the queue",
        )
    reason = note.strip()
    if not reason:
        raise ValidationFailedError(field="note", reason="say why the item needs nothing more")

    if not _mine(item, principal, now):
        # Held by someone else, this refuses by name (FR-WORK-01); a lapsed
        # hold, mine or not, is taken afresh.
        queue.claim(
            session,
            work_item_id=work_item_id,
            client_id=item.client_id,
            operator=principal.subject,
            now=now,
        )
    if item.state == str(WorkItemState.ASSIGNED):
        # Deciding is working the item: Assigned -> InProgress -> Resolved.
        queue.open_item(
            session,
            work_item_id=work_item_id,
            client_id=item.client_id,
            operator=principal.subject,
            now=now,
        )
    queue.resolve(
        session,
        work_item_id=work_item_id,
        client_id=item.client_id,
        operator=principal.subject,
        outcome=outcome,
        note=reason,
        now=now,
    )
    session.refresh(item)
    return _held(item)
