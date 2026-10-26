"""The work item state machine (M3.3).

Same shape as the claim machine's tests and for the same reason: a table can
be enumerated, so "every defined transition works and none of the others do"
is a loop rather than twelve hand-written cases.

SRS section 7.7 is explicit that this machine is *"distinct from the claim
state machine"*. The tests keep them apart too -- a denied claim and "work
the denial" look like one fact until one claim raises two items, which is
the normal case.
"""

from __future__ import annotations

import itertools

import pytest

from rcm.platform.errors import StateTransitionError
from rcm.work.state import (
    FINAL_STATES,
    HELD_STATES,
    OPEN_STATES,
    TRANSITIONS,
    WorkItemState,
    check,
    is_final,
    is_held,
    transitions_from,
)


@pytest.mark.parametrize("transition", TRANSITIONS, ids=lambda t: f"{t.source}->{t.target}")
def test_every_defined_transition_is_accepted(transition) -> None:
    assert check(transition.source, transition.target) is transition


def test_every_pair_the_table_omits_is_rejected() -> None:
    """64 ordered pairs, 12 defined. Catches a `check` that fell back to
    permitting anything it did not recognise."""
    defined = {t.key for t in TRANSITIONS}
    for source, target in itertools.product(WorkItemState, repeat=2):
        if (source, target) in defined:
            continue
        with pytest.raises(StateTransitionError):
            check(source, target)


@pytest.mark.invariant
def test_resolved_is_not_final() -> None:
    """FR-WORK-04's outcomes are reversible. An item resolved in March and
    reversed in September was resolved, correctly, in March -- the same
    shape as `Closed` on the claim machine."""
    assert not is_final(WorkItemState.RESOLVED)
    assert check(WorkItemState.RESOLVED, WorkItemState.REOPENED) is not None


def test_auto_resolved_is_the_only_final_state() -> None:
    """FR-WORK-03 counts it separately, and nothing follows it: an item the
    system handled and no human saw has no outcome left to reverse by
    hand."""
    assert {WorkItemState.AUTO_RESOLVED} == FINAL_STATES
    assert transitions_from(WorkItemState.AUTO_RESOLVED) == ()


def test_only_held_states_have_a_holder() -> None:
    """FR-WORK-01. `Assigned` and `InProgress` are the two states in which
    a named operator is holding the item, and therefore the only two a
    lease can expire from."""
    assert {WorkItemState.ASSIGNED, WorkItemState.IN_PROGRESS} == HELD_STATES
    for state in WorkItemState:
        assert is_held(state) is (state in HELD_STATES)


@pytest.mark.invariant
def test_every_held_state_can_return_to_the_queue() -> None:
    """FR-WORK-02. *"Operators go home mid-item."*

    SRS 7.7 draws the release only from `InProgress`, but an operator who
    claims an item and never opens it leaves it locked forever otherwise --
    the exact failure FR-WORK-02 exists to prevent, arriving one state
    earlier than the diagram anticipated. A held state with no way back is
    a lock that expiry cannot clear.
    """
    for state in HELD_STATES:
        assert check(state, WorkItemState.QUEUED) is not None


def test_no_item_can_become_stuck() -> None:
    for state in WorkItemState:
        if is_final(state):
            continue
        assert transitions_from(state), f"{state} has no outgoing transition and is not final"


def test_every_open_state_is_one_a_queue_query_would_find() -> None:
    """An item in a state the queue does not read is an item nobody will
    ever work, which is indistinguishable from losing it."""
    assert (
        set(WorkItemState)
        - {
            WorkItemState.RESOLVED,
            WorkItemState.AUTO_RESOLVED,
        }
        == OPEN_STATES
    )


def test_every_transition_states_a_reason() -> None:
    for transition in TRANSITIONS:
        assert transition.reason.strip()
