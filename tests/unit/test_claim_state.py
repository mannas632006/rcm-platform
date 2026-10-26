"""The claim state machine (M3.1).

    FR-BE-09 (MUST): Valid transitions are declared as data, not scattered
    through `if` statements. Undefined transitions are rejected.

The acceptance condition is *"undefined transition rejected; every defined
one tested"*, and the second half is why the transitions are a table: a list
can be enumerated, so "every defined one" is a loop rather than thirty-six
hand-written cases that drift from the diagram one forgotten edge at a time.

The structural tests matter at least as much as the per-edge ones. A typo in
the table produces a machine that still imports, still passes every
transition test, and strands a claim in a state nothing leaves.
"""

from __future__ import annotations

import itertools

import pytest

from rcm.domain.claim_state import (
    FINAL_STATES,
    INITIAL_STATE,
    OPPOSED_GUARDS,
    RESTING_STATES,
    TRANSITIONS,
    ClaimState,
    Guard,
    check,
    find,
    is_final,
    requires_next_action,
    transitions_from,
)
from rcm.platform.errors import StateTransitionError

#: Facts that satisfy every guard at once. Used where the test is about the
#: edge rather than the guard; the guards have their own section.
ALL_FACTS = {str(guard): True for guard in Guard}


# ---------------------------------------------------------------------------
# Every defined transition, and nothing else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transition", TRANSITIONS, ids=lambda t: f"{t.source}->{t.target}")
def test_every_defined_transition_is_accepted(transition) -> None:
    """The acceptance condition, enumerated rather than transcribed."""
    assert check(transition.source, transition.target, facts=ALL_FACTS) is transition


@pytest.mark.invariant
def test_an_undefined_transition_is_rejected() -> None:
    """BR-STATE-02: transitions not defined MUST be rejected, not silently
    permitted.

    `Submitted -> Paid` is the one worth naming. It is the transition
    everybody wants -- it skips adjudication and lands on the happy ending --
    and a claim that took it would be reported as paid without any
    remittance ever having arrived.
    """
    with pytest.raises(StateTransitionError) as raised:
        check(ClaimState.SUBMITTED, ClaimState.PAID, facts=ALL_FACTS)

    assert raised.value.context["reason"] == "no such transition is defined"
    # The refusal says what *is* allowed, so the caller is not left guessing.
    assert "InAdjudication" in raised.value.context["allowed"]


def test_every_pair_the_table_omits_is_rejected() -> None:
    """The complement of the table, exhaustively.

    625 ordered pairs, 36 of them defined. This is the test that would catch
    a `find()` that fell back to permitting anything it did not recognise --
    which is the single change that would make every other test here pass
    while the machine enforced nothing.
    """
    defined = {t.key for t in TRANSITIONS}
    for source, target in itertools.product(ClaimState, repeat=2):
        if (source, target) in defined:
            continue
        with pytest.raises(StateTransitionError):
            check(source, target, facts=ALL_FACTS)


def test_the_table_has_no_duplicate_edges() -> None:
    """Two rows for one pair would mean the second is unreachable, and the
    reason recorded in the audit trail would depend on lookup order."""
    keys = [t.key for t in TRANSITIONS]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# BR-STATE-03 and BR-STATE-04
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_closed_is_not_final() -> None:
    """BR-STATE-03: reversals and recoupments reopen a claim months later.

    A claim closed in March and recouped in September was closed, correctly,
    in March. Modelling `Closed` as final would make the platform unable to
    represent what actually happened -- and M1.10 already loads those
    reversals, so this is not hypothetical.
    """
    assert not is_final(ClaimState.CLOSED)
    assert find(ClaimState.CLOSED, ClaimState.REOPENED) is not None
    assert check(ClaimState.CLOSED, ClaimState.REOPENED) is not None


def test_cancelled_is_the_only_final_state() -> None:
    assert {ClaimState.CANCELLED} == FINAL_STATES
    assert transitions_from(ClaimState.CANCELLED) == ()


@pytest.mark.invariant
def test_claim_never_rests_without_next_action() -> None:
    """BR-STATE-04: no claim may remain in a non-terminal state without a
    scheduled next action.

    The states a claim may rest in are exactly the two it has finished
    moving through. Every other state is somewhere a claim is *passing*, and
    a claim that has sat in `AuthPending` for six weeks because nobody
    scheduled a follow-up is the commonest way money is lost quietly.
    """
    for state in ClaimState:
        assert requires_next_action(state) is (state not in RESTING_STATES)

    assert not requires_next_action(ClaimState.CLOSED)
    assert not requires_next_action(ClaimState.CANCELLED)
    assert requires_next_action(ClaimState.AUTH_PENDING)
    assert requires_next_action(ClaimState.IN_ADJUDICATION)
    assert requires_next_action(ClaimState.REOPENED)


def test_resting_is_not_the_same_as_final() -> None:
    """The distinction, stated as a test because it is one line from being
    collapsed. `Closed` rests and is not final; a refactor that defined
    `requires_next_action` as `not is_final` would demand a follow-up date
    on every closed claim forever."""
    assert ClaimState.CLOSED in RESTING_STATES
    assert ClaimState.CLOSED not in FINAL_STATES


# ---------------------------------------------------------------------------
# The shape of the machine
# ---------------------------------------------------------------------------


def test_every_state_is_reachable_from_the_initial_state() -> None:
    """A state nothing reaches is a state the diagram and the table disagree
    about -- and it imports, and every other test here passes."""
    reached = {INITIAL_STATE}
    frontier = [INITIAL_STATE]
    while frontier:
        for transition in transitions_from(frontier.pop()):
            if transition.target not in reached:
                reached.add(transition.target)
                frontier.append(transition.target)

    unreachable = set(ClaimState) - reached
    assert not unreachable, f"unreachable from {INITIAL_STATE}: {sorted(unreachable)}"


def test_no_claim_can_become_stuck() -> None:
    """Every non-final state has somewhere to go.

    A state with no outgoing edge that is not declared final is a trap: the
    claim cannot advance and cannot be closed, so BR-STATE-04 demands a next
    action that can never be taken.
    """
    for state in ClaimState:
        if is_final(state):
            continue
        assert transitions_from(state), f"{state} has no outgoing transition and is not final"


def test_every_transition_states_a_reason() -> None:
    """BR-STATE-01 logs a reason with every transition. An empty one would
    satisfy the schema and tell an operator nothing."""
    for transition in TRANSITIONS:
        assert transition.reason.strip()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_a_guarded_transition_is_refused_without_its_fact() -> None:
    """PD-18. Absence is not permission.

    Treating a missing fact as false would route every claim down the
    no-authorisation branch of a machine that was never asked the question --
    and the claim would be submitted without an authorisation nobody checked
    for.
    """
    with pytest.raises(StateTransitionError) as raised:
        check(ClaimState.ELIGIBILITY_VERIFIED, ClaimState.AUTH_PENDING, facts={})

    assert raised.value.context["guard"] == Guard.AUTH_REQUIRED


def test_a_guarded_transition_is_refused_when_its_fact_is_false() -> None:
    with pytest.raises(StateTransitionError):
        check(
            ClaimState.PAID,
            ClaimState.SECONDARY_BILLING,
            facts={str(Guard.SECONDARY_COVERAGE_EXISTS): False},
        )


def test_a_guarded_transition_is_allowed_when_its_fact_holds() -> None:
    transition = check(
        ClaimState.PAID,
        ClaimState.SECONDARY_BILLING,
        facts={str(Guard.SECONDARY_COVERAGE_EXISTS): True},
    )
    assert transition.guard is Guard.SECONDARY_COVERAGE_EXISTS


def test_an_unguarded_transition_needs_no_facts() -> None:
    assert check(ClaimState.REGISTERED, ClaimState.ELIGIBILITY_VERIFIED) is not None


@pytest.mark.parametrize(("yes", "no"), OPPOSED_GUARDS)
def test_opposed_guards_split_a_state_exhaustively(yes: Guard, no: Guard) -> None:
    """Each pair of opposed guards leaves its source state by exactly one
    edge for any given answer.

    If both edges were satisfiable at once the machine would be
    non-deterministic; if neither were, the claim would be stuck in a state
    the diagram shows as a fork.
    """
    sources = {t.source for t in TRANSITIONS if t.guard in {yes, no}}
    for source in sources:
        for answer in (True, False):
            facts = {str(yes): answer, str(no): not answer}
            open_edges = [
                t
                for t in transitions_from(source)
                if t.guard is None or facts.get(str(t.guard), False)
            ]
            assert len(open_edges) == 1, (
                f"{source} with {yes}={answer} has {len(open_edges)} open edges"
            )
