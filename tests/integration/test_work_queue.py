"""The work queue, its exclusive locks and their expiry (M3.3, M3.4).

    FR-WORK-01 (MUST): Assignment places an exclusive lock. Two operators
    must never work one item (UR-04).

    FR-WORK-02 (MUST): Assignment locks expire after `[TBD-57]` minutes of
    inactivity and return the item to the queue. Operators go home
    mid-item.

Every test passes the clock in. A lock that expires "in thirty minutes" is
untestable if thirty minutes have to pass, and a test that sleeps is slow
and still flaky -- it asserts on wall-clock timing that a loaded CI runner
is free to disagree with.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text

from rcm.platform.audit import AuditEvent
from rcm.platform.config import get_settings
from rcm.platform.db import reset_engines, session_factory, session_scope
from rcm.platform.errors import (
    ResourceNotFoundError,
    StateTransitionError,
    ValidationFailedError,
    WorkItemLockedError,
)
from rcm.work import queue
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import WorkItemOutcome, WorkItemState

pytestmark = pytest.mark.integration

CLIENT = uuid.UUID("4a4a4a4a-0000-4000-8000-00000000004a")
OTHER_CLIENT = uuid.UUID("4b4b4b4b-0000-4000-8000-00000000004b")
NOON = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text("TRUNCATE work_item RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def item_id() -> uuid.UUID:
    with session_scope() as session:
        item = queue.create(
            session,
            client_id=CLIENT,
            kind=WorkItemKind.DENIAL,
            title="CO-50 medical necessity on CLM-001 line 1",
            owner_role="denials_specialist",
            priority_score=40,
        )
        return item.work_item_id


def state_of(item_id: uuid.UUID) -> WorkItemState:
    with session_scope() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        return WorkItemState(item.state)


# ---------------------------------------------------------------------------
# FR-WORK-01
# ---------------------------------------------------------------------------


def test_claiming_takes_the_item(item_id: uuid.UUID) -> None:
    with session_scope() as session:
        lease = queue.claim(
            session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON
        )

    assert lease.operator == "alice"
    assert lease.expires_at == NOON + timedelta(minutes=30)
    assert state_of(item_id) is WorkItemState.ASSIGNED


@pytest.mark.invariant
def test_two_operators_cannot_hold_one_item(item_id: uuid.UUID) -> None:
    """FR-WORK-01 / UR-04, sequentially.

    The second operator is refused *by name*: told who holds it and until
    when, so the answer to "why can't I take this" is a sentence rather
    than a disabled button.
    """
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)

    with pytest.raises(WorkItemLockedError) as raised, session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="bob", now=NOON)

    assert raised.value.context["held_by"] == "alice"
    with session_scope() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        assert item.assigned_to == "alice"


@pytest.mark.invariant
def test_simultaneous_claims_leave_exactly_one_holder(item_id: uuid.UUID) -> None:
    """The race the sequential test cannot reach.

    Both operators read the item, both see no holder, both write their own
    name -- and the column holds one name, so nothing in the row reveals
    that it happened. The first operator's screen still shows the item,
    they work it, and two people call the payer about the same denial.

    The lock is taken before the state is read, so the loser waits,
    re-reads, and finds a holder.
    """
    maker = session_factory()
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def contend(operator: str) -> None:
        session = maker()
        try:
            barrier.wait(timeout=10)
            queue.claim(
                session, work_item_id=item_id, client_id=CLIENT, operator=operator, now=NOON
            )
            session.commit()
            with lock:
                results.append(f"{operator}:won")
        except WorkItemLockedError:
            session.rollback()
            with lock:
                results.append(f"{operator}:refused")
        finally:
            session.close()

    threads = [threading.Thread(target=contend, args=(name,)) for name in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    won = [r for r in results if r.endswith(":won")]
    refused = [r for r in results if r.endswith(":refused")]
    assert len(won) == 1, f"expected exactly one winner, got {results}"
    assert len(refused) == 1, f"expected exactly one refusal, got {results}"

    with session_scope() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        assert f"{item.assigned_to}:won" == won[0]


def test_only_the_holder_may_open_resolve_or_release(item_id: uuid.UUID) -> None:
    """A second operator acting on a held item is the same violation
    arriving by a different door than a double claim."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)

    for action in (queue.open_item, queue.release):
        with pytest.raises(WorkItemLockedError), session_scope() as session:
            action(session, work_item_id=item_id, client_id=CLIENT, operator="bob", now=NOON)

    with pytest.raises(WorkItemLockedError), session_scope() as session:
        queue.resolve(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="bob",
            outcome=WorkItemOutcome.WRITTEN_OFF,
            now=NOON,
        )


def test_another_clients_item_cannot_be_claimed(item_id: uuid.UUID) -> None:
    """PD-20. The client is part of the lookup, so an item id alone is
    never enough."""
    with pytest.raises(ResourceNotFoundError), session_scope() as session:
        queue.claim(
            session, work_item_id=item_id, client_id=OTHER_CLIENT, operator="intruder", now=NOON
        )


# ---------------------------------------------------------------------------
# FR-WORK-02
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_an_expired_lease_returns_the_item_to_the_queue(item_id: uuid.UUID) -> None:
    """FR-WORK-02. *"Operators go home mid-item."*

    A lock given up only deliberately accumulates, and the queue fills with
    items nobody is working and nobody can take.
    """
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)

    # One minute before the lease ends: still held.
    with session_scope() as session:
        assert (
            queue.expire_leases(session, client_id=CLIENT, now=NOON + timedelta(minutes=29)) == []
        )
    assert state_of(item_id) is WorkItemState.ASSIGNED

    with session_scope() as session:
        released = queue.expire_leases(session, client_id=CLIENT, now=NOON + timedelta(minutes=31))
    assert released == [item_id]
    assert state_of(item_id) is WorkItemState.QUEUED

    with session_scope() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        # Both halves cleared: a name without a lease is a holder nobody
        # can time out, and the schema constraint refuses it besides.
        assert item.assigned_to is None
        assert item.lock_expires_at is None


def test_renewing_the_lease_keeps_the_item(item_id: uuid.UUID) -> None:
    """Working on something is what keeps it held."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    with session_scope() as session:
        queue.renew(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="alice",
            now=NOON + timedelta(minutes=20),
        )

    with session_scope() as session:
        assert (
            queue.expire_leases(session, client_id=CLIENT, now=NOON + timedelta(minutes=31)) == []
        )
    assert state_of(item_id) is WorkItemState.ASSIGNED


def test_only_the_holder_may_renew(item_id: uuid.UUID) -> None:
    """A renewal by a second operator is a silent transfer of the lock."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    with pytest.raises(WorkItemLockedError), session_scope() as session:
        queue.renew(session, work_item_id=item_id, client_id=CLIENT, operator="bob", now=NOON)


def test_an_item_held_past_its_lease_can_be_taken_by_someone_else(item_id: uuid.UUID) -> None:
    """Expiry is evaluated on claim as well as by the sweep, so an operator
    taking over from someone who went home does not wait for a background
    job to notice."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)

    with session_scope() as session:
        lease = queue.claim(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="bob",
            now=NOON + timedelta(minutes=31),
        )
    assert lease.operator == "bob"


def test_expiry_does_not_touch_items_nobody_holds(item_id: uuid.UUID) -> None:
    """A queued item has no lease to expire. A sweep that returned it would
    be rewriting rows for no reason and auditing a release nobody made."""
    with session_scope() as session:
        assert queue.expire_leases(session, client_id=CLIENT, now=NOON + timedelta(days=30)) == []
    assert state_of(item_id) is WorkItemState.QUEUED


# ---------------------------------------------------------------------------
# FR-WORK-03 and FR-WORK-04
# ---------------------------------------------------------------------------


def test_resolving_records_a_structured_outcome(item_id: uuid.UUID) -> None:
    """FR-WORK-04. The note sits beside the outcome, never instead of it."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    with session_scope() as session:
        queue.open_item(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    with session_scope() as session:
        queue.resolve(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="alice",
            outcome=WorkItemOutcome.CORRECTED_AND_RESUBMITTED,
            now=NOON + timedelta(minutes=5),
            note="member id had a transposed digit",
        )

    with session_scope() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        assert item.state == str(WorkItemState.RESOLVED)
        assert item.outcome == str(WorkItemOutcome.CORRECTED_AND_RESUBMITTED)
        assert item.resolution_note == "member id had a transposed digit"
        # Cleared, or the item stays inside the sweep's reach forever.
        assert item.assigned_to is None
        assert item.lock_expires_at is None


@pytest.mark.invariant
def test_auto_resolved_is_a_separate_state_from_resolved(item_id: uuid.UUID) -> None:
    """FR-WORK-03: *"mixing them with human resolutions makes automation
    coverage unmeasurable."*

    An item the system handled and nobody saw is the measure of how much
    work the platform is actually removing. It is only countable if it is a
    different value, not a flag somebody has to remember to filter on.
    """
    with session_scope() as session:
        queue.auto_resolve(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            outcome=WorkItemOutcome.NO_ACTION_NEEDED,
            now=NOON,
        )

    assert state_of(item_id) is WorkItemState.AUTO_RESOLVED

    with session_scope() as session:
        human = session.scalars(
            select(WorkItem).where(WorkItem.state == str(WorkItemState.RESOLVED))
        ).all()
        automated = session.scalars(
            select(WorkItem).where(WorkItem.state == str(WorkItemState.AUTO_RESOLVED))
        ).all()
    assert len(human) == 0
    assert len(automated) == 1


def test_an_item_a_human_touched_cannot_be_auto_resolved(item_id: uuid.UUID) -> None:
    """`Queued -> AutoResolved` is defined; `Assigned -> AutoResolved` is
    not. Once a human has it, resolving it is a human resolution, and
    counting it as automation would overstate the coverage FR-WORK-03
    exists to measure."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)

    with pytest.raises(StateTransitionError), session_scope() as session:
        queue.auto_resolve(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            outcome=WorkItemOutcome.NO_ACTION_NEEDED,
            now=NOON,
        )


@pytest.mark.invariant
def test_a_released_item_cannot_be_auto_resolved(item_id: uuid.UUID) -> None:
    """FR-WORK-03. A released item is back in `Queued`, the one state
    `AutoResolved` is reachable from -- so the state alone would let a
    human's work be counted as automation. `first_claimed_at` remembers."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    with session_scope() as session:
        queue.release(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    assert state_of(item_id) is WorkItemState.QUEUED

    with pytest.raises(StateTransitionError) as raised, session_scope() as session:
        queue.auto_resolve(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            outcome=WorkItemOutcome.NO_ACTION_NEEDED,
            now=NOON,
        )
    assert "FR-WORK-03" in raised.value.context["reason"]
    assert state_of(item_id) is WorkItemState.QUEUED


# ---------------------------------------------------------------------------
# [TBD-57] and the audit trail
# ---------------------------------------------------------------------------


@pytest.fixture
def lease_setting(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RCM_WORK_ITEM_LOCK_EXPIRY_MINUTES", "7")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("RCM_WORK_ITEM_LOCK_EXPIRY_MINUTES")
    get_settings.cache_clear()


def test_the_lease_length_comes_from_settings(item_id: uuid.UUID, lease_setting: None) -> None:
    """[TBD-57] is a placeholder the report says can be set. Setting it has
    to change the lease, or the report is lying."""
    with session_scope() as session:
        lease = queue.claim(
            session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON
        )
    assert lease.expires_at == NOON + timedelta(minutes=7)


@pytest.mark.parametrize("minutes", [0, -5])
def test_a_lease_must_last_at_least_a_minute(item_id: uuid.UUID, minutes: int) -> None:
    """A lease that is already over when granted is a lock that does nothing."""
    with pytest.raises(ValidationFailedError), session_scope() as session:
        queue.claim(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="alice",
            now=NOON,
            lock_minutes=minutes,
        )
    assert state_of(item_id) is WorkItemState.QUEUED


def audit_actions(item_id: uuid.UUID) -> list[str]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.entity_id == item_id)
                .order_by(AuditEvent.occurred_at, AuditEvent.action)
            ).all()
        )


def test_every_change_to_an_item_is_audited(item_id: uuid.UUID) -> None:
    """Who held an item, when, and what they did with it is the record a
    dispute is settled from. A release with no audit row is a gap in it."""
    later = NOON + timedelta(minutes=5)
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    with session_scope() as session:
        queue.release(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="bob", now=NOON)
    with session_scope() as session:
        queue.renew(session, work_item_id=item_id, client_id=CLIENT, operator="bob", now=later)
    with session_scope() as session:
        queue.open_item(session, work_item_id=item_id, client_id=CLIENT, operator="bob", now=later)
    with session_scope() as session:
        queue.resolve(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="bob",
            outcome=WorkItemOutcome.APPEALED,
            now=later,
        )

    assert sorted(audit_actions(item_id)) == sorted(
        [
            "work_item.created",
            "work_item.claimed",
            "work_item.released",
            "work_item.claimed",
            "work_item.lease_renewed",
            "work_item.opened",
            "work_item.resolved",
        ]
    )


def test_taking_over_an_expired_lease_records_the_expiry(item_id: uuid.UUID) -> None:
    """Without this the audit says bob took the item from the queue, and
    nothing shows alice had been holding it."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    with session_scope() as session:
        queue.claim(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="bob",
            now=NOON + timedelta(minutes=31),
        )

    with session_scope() as session:
        expiry = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == item_id, AuditEvent.action == "work_item.lease_expired"
            )
        ).all()
    assert len(expiry) == 1
    assert expiry[0].before_state == {"assigned_to": "alice"}
    # bob's claim is what ended alice's lease, so the record names bob.
    assert expiry[0].actor_id == "bob"


def test_a_holder_whose_lease_ran_out_cannot_keep_working(item_id: uuid.UUID) -> None:
    """FR-WORK-02. Before the sweep runs, the row still names alice. Her late
    renew, open or resolve must be refused, or work lands on an item the
    queue already treats as free."""
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=NOON)
    late = NOON + timedelta(minutes=31)

    for action in (queue.renew, queue.open_item):
        with pytest.raises(WorkItemLockedError), session_scope() as session:
            action(session, work_item_id=item_id, client_id=CLIENT, operator="alice", now=late)

    with pytest.raises(WorkItemLockedError), session_scope() as session:
        queue.resolve(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="alice",
            outcome=WorkItemOutcome.APPEALED,
            now=late,
        )
    assert state_of(item_id) is WorkItemState.ASSIGNED
