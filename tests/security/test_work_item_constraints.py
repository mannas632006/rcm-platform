"""The database refuses a work item that breaks FR-WORK-01 or FR-WORK-04.

Two of these constraints are written as **equivalences** rather than
implications, which is the part worth reading twice. Both directions carry
weight, and an implication in either direction alone permits a real failure.

Every assertion goes through the **application** role, because that is the
identity the platform runs as.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.security

CLIENT = uuid.UUID("4c4c4c4c-0000-4000-8000-00000000004c")


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text("TRUNCATE work_item RESTART IDENTITY CASCADE"))

    wipe()
    yield
    wipe()


def _insert(conn: object, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "client": CLIENT,
        "kind": "DENIAL",
        "state": "Queued",
        "title": "CO-50 medical necessity on CLM-001 line 1",
        "owner_role": "denials_specialist",
        "score": 40,
        "assigned_to": None,
        "lock_expires_at": None,
        "outcome": None,
    }
    values.update(overrides)
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO work_item (work_item_id, client_id, kind, state, title, "
            "owner_role, priority_score, assigned_to, lock_expires_at, outcome) "
            "VALUES (:id, :client, :kind, :state, :title, :owner_role, :score, "
            ":assigned_to, :lock_expires_at, :outcome)"
        ),
        values,
    )


def test_a_well_formed_item_is_accepted(app_engine: Engine) -> None:
    with app_engine.begin() as conn:
        _insert(conn)


# ---------------------------------------------------------------------------
# FR-WORK-01, both directions
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_held_item_must_name_its_holder(app_engine: Engine) -> None:
    """An item in a held state with no holder is a lock nobody can see: the
    queue will not offer it, and no operator can be told who has it."""
    with (
        pytest.raises(IntegrityError, match="a_held_item_names_its_holder_and_its_lease"),
        app_engine.begin() as conn,
    ):
        _insert(conn, state="Assigned", assigned_to=None, lock_expires_at=None)


@pytest.mark.invariant
def test_a_held_item_must_carry_a_lease(app_engine: Engine) -> None:
    """FR-WORK-02 cannot act on a holder with no expiry. The item would
    stay held until somebody noticed by hand, which is the failure the
    lease exists to prevent."""
    with (
        pytest.raises(IntegrityError, match="a_held_item_names_its_holder_and_its_lease"),
        app_engine.begin() as conn,
    ):
        _insert(conn, state="Assigned", assigned_to="alice", lock_expires_at=None)


def test_an_unheld_item_must_not_name_a_holder(app_engine: Engine) -> None:
    """The other direction, and the reason the constraint is an equivalence.

    A name on a queued item is a lease the sweep will never look at --
    `expire_leases` reads only held states -- so it would sit there
    claiming an owner the queue is simultaneously offering to everyone.
    """
    with (
        pytest.raises(IntegrityError, match="a_held_item_names_its_holder_and_its_lease"),
        app_engine.begin() as conn,
    ):
        _insert(conn, state="Queued", assigned_to="alice", lock_expires_at="2026-06-01 12:00+00")


# ---------------------------------------------------------------------------
# FR-WORK-04, both directions
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_resolved_item_must_record_its_outcome(app_engine: Engine) -> None:
    """FR-WORK-04: *"Outcomes are the training signal."*

    "Resolved, reason unknown" is the row that makes the signal useless,
    and M4.9's feedback loop -- preventable denial becomes a candidate rule
    -- has nothing to learn from it.
    """
    with (
        pytest.raises(IntegrityError, match="a_resolved_item_records_its_outcome"),
        app_engine.begin() as conn,
    ):
        _insert(conn, state="Resolved", outcome=None)


def test_an_unresolved_item_must_not_record_an_outcome(app_engine: Engine) -> None:
    """A conclusion recorded before the work was done. It would be counted
    by every throughput and outcome query while the item is still sitting
    in the queue."""
    with (
        pytest.raises(IntegrityError, match="a_resolved_item_records_its_outcome"),
        app_engine.begin() as conn,
    ):
        _insert(conn, state="Queued", outcome="WRITTEN_OFF")


def test_both_resolved_states_require_an_outcome(app_engine: Engine) -> None:
    """FR-WORK-03's separate state is not an exemption: an automated
    resolution still records what it did."""
    with (
        pytest.raises(IntegrityError, match="a_resolved_item_records_its_outcome"),
        app_engine.begin() as conn,
    ):
        _insert(conn, state="AutoResolved", outcome=None)


# ---------------------------------------------------------------------------
# BR-EXC-01 and BR-EXC-02
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_an_item_must_name_the_specific_problem(app_engine: Engine) -> None:
    """BR-EXC-01: *"Every exception MUST name the specific problem and the
    specific record. Generic failure messages are defects."*

    The empty title is the degenerate case the constraint can actually
    catch; "an error occurred" is the one review has to catch.
    """
    with (
        pytest.raises(IntegrityError, match="work_item_names_its_problem"),
        app_engine.begin() as conn,
    ):
        _insert(conn, title="")


def test_an_item_must_name_an_owner(app_engine: Engine) -> None:
    """BR-EXC-02: every exception has an owner and an SLA. An unowned item
    is one every role assumes another role is working."""
    with (
        pytest.raises(IntegrityError, match="work_item_names_its_owner"),
        app_engine.begin() as conn,
    ):
        _insert(conn, owner_role="")


@pytest.mark.parametrize("state", ["Done", "assigned", "", "Closed"])
def test_an_unknown_state_is_refused(app_engine: Engine, state: str) -> None:
    """`Closed` is in the parameters on purpose: it is a valid *claim*
    state and not a work item state, and the two machines are distinct.
    Accepting it here would let a claim's vocabulary leak into the queue."""
    with (
        pytest.raises(IntegrityError, match="work_item_state_is_known"),
        app_engine.begin() as conn,
    ):
        _insert(conn, state=state)


@pytest.mark.parametrize("score", [-1, 101])
def test_a_priority_outside_the_scale_is_refused(app_engine: Engine, score: int) -> None:
    """FR-WORK-05 reserves 100 for deadline-pinned items. A score above it
    would outrank an expiring claim, which is PD-15 inverted."""
    with (
        pytest.raises(IntegrityError, match="priority_score_is_a_percentage"),
        app_engine.begin() as conn,
    ):
        _insert(conn, score=score)
