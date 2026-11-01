"""Claiming, holding and releasing work (M3.3, M3.4).

    FR-WORK-01 (MUST): Assignment places an exclusive lock. Two operators
    must never work one item (UR-04).

    FR-WORK-02 (MUST): Assignment locks expire after `[TBD-57]` minutes of
    inactivity and return the item to the queue.

**Why `assigned_to is None` is not enough.** Two operators press "take next"
at the same instant. Both read the item, both see no holder, both write
their own name, and the second write wins silently. The first operator's
screen still shows the item, they work it, and two people spend an hour on
the same denial -- or worse, both call the payer. The column can only hold
one name, which is exactly why nothing in the row reveals that this
happened.

So claiming takes a row lock and re-reads inside it. The loser does not
overwrite; it finds the item already held and is told so by name.

**Time is a parameter, never `now()`.** Every function here takes the
current time from the caller. A lock that expires "in thirty minutes" is
untestable if thirty minutes have to pass, and a test that sleeps is a test
that is slow and still flaky. Passing the clock in makes expiry an ordinary
assertion about an ordinary function.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.errors import ResourceNotFoundError, WorkItemLockedError
from rcm.platform.logging import get_logger
from rcm.work.models import DEFAULT_LOCK_MINUTES, WorkItem, WorkItemKind
from rcm.work.state import (
    WorkItemOutcome,
    WorkItemState,
    check,
    is_held,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Lease:
    """Who holds an item, and until when."""

    work_item_id: uuid.UUID
    operator: str
    expires_at: datetime


def create(
    session: Session,
    *,
    client_id: uuid.UUID,
    kind: WorkItemKind,
    title: str,
    owner_role: str,
    claim_id: uuid.UUID | None = None,
    detail: str = "",
    priority_score: int = 0,
    deadline_pinned: bool = False,
    sla_due_at: datetime | None = None,
) -> WorkItem:
    """Raise a work item, queued and ready to be claimed.

    Items are created straight into `Queued` rather than `Created`: the
    intermediate state exists in SRS 7.7 for an item awaiting a priority
    score, and the score is computed here. An item left in `Created` is one
    no queue query would return, which is the same as losing it.
    """
    item = WorkItem(
        client_id=client_id,
        claim_id=claim_id,
        kind=str(kind),
        state=str(WorkItemState.QUEUED),
        title=title,
        detail=detail,
        priority_score=priority_score,
        deadline_pinned=deadline_pinned,
        owner_role=owner_role,
        sla_due_at=sla_due_at,
    )
    session.add(item)
    session.flush()

    log.info(
        "work_item.created",
        kind=str(kind),
        owner_role=owner_role,
        deadline_pinned=deadline_pinned,
    )
    return item


def _locked(session: Session, *, work_item_id: uuid.UUID, client_id: uuid.UUID) -> WorkItem:
    """Read one item with its row locked.

    PD-20: the client is part of the lookup, not a filter applied after.
    """
    item = session.scalar(
        select(WorkItem)
        .where(WorkItem.work_item_id == work_item_id, WorkItem.client_id == client_id)
        .with_for_update()
    )
    if item is None:
        raise ResourceNotFoundError(entity="work_item", entity_id=str(work_item_id))
    return item


def claim(
    session: Session,
    *,
    work_item_id: uuid.UUID,
    client_id: uuid.UUID,
    operator: str,
    now: datetime,
    lock_minutes: int = DEFAULT_LOCK_MINUTES,
) -> Lease:
    """Take exclusive hold of an item (FR-WORK-01).

    The row is locked *before* its state is read, so two simultaneous
    claims cannot both see an unheld item. The loser waits, re-reads, finds
    a holder, and is refused by name rather than silently overwriting.
    """
    item = _locked(session, work_item_id=work_item_id, client_id=client_id)
    state = WorkItemState(item.state)

    if is_held(state):
        # Expiry is evaluated here rather than only by the sweep, so an
        # operator taking an item whose previous holder went home does not
        # have to wait for a background job to notice.
        if item.lock_expires_at is not None and item.lock_expires_at <= now:
            _release(session, item, now=now, reason="lease expired")
            state = WorkItemState(item.state)
        else:
            raise WorkItemLockedError(
                work_item_id=str(work_item_id),
                held_by=item.assigned_to or "unknown",
                expires_at=item.lock_expires_at.isoformat() if item.lock_expires_at else "",
            )

    check(state, WorkItemState.ASSIGNED)

    expires_at = now + timedelta(minutes=lock_minutes)
    item.state = str(WorkItemState.ASSIGNED)
    item.assigned_to = operator
    item.lock_expires_at = expires_at

    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=operator,
        action="work_item.claimed",
        entity_type="work_item",
        entity_id=work_item_id,
        before_state={"state": str(state)},
        after_state={"state": str(WorkItemState.ASSIGNED), "assigned_to": operator},
    )
    session.flush()

    log.info("work_item.claimed", work_item_id=str(work_item_id), lock_minutes=lock_minutes)
    return Lease(work_item_id=work_item_id, operator=operator, expires_at=expires_at)


def renew(
    session: Session,
    *,
    work_item_id: uuid.UUID,
    client_id: uuid.UUID,
    operator: str,
    now: datetime,
    lock_minutes: int = DEFAULT_LOCK_MINUTES,
) -> Lease:
    """Extend the lease. Working on something is what keeps it held.

    Refused for anyone but the current holder: a renewal by a second
    operator would be a silent transfer of the lock, which is FR-WORK-01
    broken by a different route than a double claim.
    """
    item = _locked(session, work_item_id=work_item_id, client_id=client_id)

    if item.assigned_to != operator:
        raise WorkItemLockedError(
            work_item_id=str(work_item_id),
            held_by=item.assigned_to or "nobody",
            reason="only the current holder may renew the lease",
        )

    expires_at = now + timedelta(minutes=lock_minutes)
    item.lock_expires_at = expires_at
    session.flush()
    return Lease(work_item_id=work_item_id, operator=operator, expires_at=expires_at)


def _release(session: Session, item: WorkItem, *, now: datetime, reason: str) -> None:
    """Return a held item to the queue, clearing the lease.

    Shared by the deliberate release and the expiry sweep, because from the
    queue's point of view they are the same event: nobody is holding this
    any more. Clearing both `assigned_to` and `lock_expires_at` keeps the
    schema's held-item constraint satisfied -- a name without a lease is a
    holder nobody can time out.
    """
    del now
    check(WorkItemState(item.state), WorkItemState.QUEUED)
    item.state = str(WorkItemState.QUEUED)
    item.assigned_to = None
    item.lock_expires_at = None
    log.info("work_item.released", work_item_id=str(item.work_item_id), reason=reason)


def release(
    session: Session,
    *,
    work_item_id: uuid.UUID,
    client_id: uuid.UUID,
    operator: str,
    now: datetime,
) -> None:
    """Give an item back deliberately."""
    item = _locked(session, work_item_id=work_item_id, client_id=client_id)
    if item.assigned_to != operator:
        raise WorkItemLockedError(
            work_item_id=str(work_item_id),
            held_by=item.assigned_to or "nobody",
            reason="only the current holder may release the item",
        )
    _release(session, item, now=now, reason="released by the operator")
    session.flush()


def expire_leases(session: Session, *, client_id: uuid.UUID, now: datetime) -> list[uuid.UUID]:
    """Return every item whose lease has run out to the queue (FR-WORK-02).

    *"Operators go home mid-item."* A lock that is only given up
    deliberately accumulates, and the queue fills with items nobody is
    working and nobody can take.

    The rows are locked as they are read. A sweep that read first and wrote
    afterwards could expire a lease the holder renewed in between, taking
    an item away from somebody actively working it.
    """
    expired = session.scalars(
        select(WorkItem)
        .where(
            WorkItem.client_id == client_id,
            WorkItem.state.in_(
                [str(s) for s in (WorkItemState.ASSIGNED, WorkItemState.IN_PROGRESS)]
            ),
            WorkItem.lock_expires_at.is_not(None),
            WorkItem.lock_expires_at <= now,
        )
        .with_for_update()
    ).all()

    released: list[uuid.UUID] = []
    for item in expired:
        previous_holder = item.assigned_to
        _release(session, item, now=now, reason="lease expired")
        released.append(item.work_item_id)
        record_audit_event(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id="system:lease_sweeper",
            action="work_item.lease_expired",
            entity_type="work_item",
            entity_id=item.work_item_id,
            before_state={"assigned_to": previous_holder},
            after_state={"state": str(WorkItemState.QUEUED)},
        )

    if released:
        session.flush()
        log.info("work_item.leases_expired", count=len(released))
    return released


def open_item(
    session: Session,
    *,
    work_item_id: uuid.UUID,
    client_id: uuid.UUID,
    operator: str,
    now: datetime,
    lock_minutes: int = DEFAULT_LOCK_MINUTES,
) -> None:
    """Assigned -> InProgress. Opening the item renews the lease."""
    item = _locked(session, work_item_id=work_item_id, client_id=client_id)
    if item.assigned_to != operator:
        raise WorkItemLockedError(
            work_item_id=str(work_item_id),
            held_by=item.assigned_to or "nobody",
            reason="only the current holder may open the item",
        )
    check(WorkItemState(item.state), WorkItemState.IN_PROGRESS)
    item.state = str(WorkItemState.IN_PROGRESS)
    item.lock_expires_at = now + timedelta(minutes=lock_minutes)
    session.flush()


def resolve(
    session: Session,
    *,
    work_item_id: uuid.UUID,
    client_id: uuid.UUID,
    operator: str,
    outcome: WorkItemOutcome,
    now: datetime,
    note: str = "",
) -> None:
    """Record a structured outcome and close the item (FR-WORK-04).

    The lease is cleared, because a resolved item has no holder -- leaving
    one would keep it inside the expiry sweep's reach forever.
    """
    item = _locked(session, work_item_id=work_item_id, client_id=client_id)
    if item.assigned_to != operator:
        raise WorkItemLockedError(
            work_item_id=str(work_item_id),
            held_by=item.assigned_to or "nobody",
            reason="only the current holder may resolve the item",
        )

    check(WorkItemState(item.state), WorkItemState.RESOLVED)
    item.state = str(WorkItemState.RESOLVED)
    item.outcome = str(outcome)
    item.resolution_note = note
    item.resolved_at = now
    item.assigned_to = None
    item.lock_expires_at = None

    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=operator,
        action="work_item.resolved",
        entity_type="work_item",
        entity_id=work_item_id,
        after_state={"outcome": str(outcome)},
    )
    session.flush()
    log.info("work_item.resolved", outcome=str(outcome))


def auto_resolve(
    session: Session,
    *,
    work_item_id: uuid.UUID,
    client_id: uuid.UUID,
    outcome: WorkItemOutcome,
    now: datetime,
    actor_id: str = "system:auto_resolver",
) -> None:
    """Resolve without a human ever holding it (FR-WORK-03).

    A separate state, not a flag on `Resolved`, because *"mixing them with
    human resolutions makes automation coverage unmeasurable"*. An item the
    system handled and nobody saw is the measure of how much work the
    platform is actually removing, and it is only countable if it is a
    different value.
    """
    item = _locked(session, work_item_id=work_item_id, client_id=client_id)
    check(WorkItemState(item.state), WorkItemState.AUTO_RESOLVED)

    item.state = str(WorkItemState.AUTO_RESOLVED)
    item.outcome = str(outcome)
    item.resolved_at = now

    record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="work_item.auto_resolved",
        entity_type="work_item",
        entity_id=work_item_id,
        after_state={"outcome": str(outcome)},
    )
    session.flush()
