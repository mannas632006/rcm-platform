"""The work item state machine, declared as data (M3.3).

SRS section 7.7 is explicit that this is *"distinct from the claim state
machine. A claim may generate many work items over its life."* They are
separate because they answer different questions: the claim's state is where
the money is, and a work item's state is whether a person is currently
holding it.

Conflating them is tempting -- a denied claim and "work the denial" look
like the same fact -- and it breaks as soon as one claim raises two items,
which is the normal case: a denial that is both an eligibility problem and a
coding problem is two pieces of work for two different people.

The shape repeats a lesson from the claim machine:

    `Resolved` is not final.

    FR-WORK-04: Every resolution records a structured outcome, not free
    text. Outcomes are the training signal.

An outcome reversed months later reopens the item. `AutoResolved` is the one
terminal state, and it is terminal because FR-WORK-03 counts it separately:
*"mixing them with human resolutions makes automation coverage
unmeasurable"*. An item the system resolved and a human never saw is the
measure of how much work the platform is actually removing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from rcm.platform.errors import StateTransitionError


class WorkItemState(StrEnum):
    """Every state a work item can be in (SRS section 7.7)."""

    CREATED = "Created"
    QUEUED = "Queued"
    ASSIGNED = "Assigned"
    IN_PROGRESS = "InProgress"
    ESCALATED = "Escalated"
    #: A human recorded an outcome. Not final: FR-WORK-04's outcomes are
    #: reversible, and a reopened item is how that is represented.
    RESOLVED = "Resolved"
    REOPENED = "Reopened"
    #: The system resolved it before any human touched it. Terminal, and
    #: counted separately (FR-WORK-03).
    AUTO_RESOLVED = "AutoResolved"


class WorkItemOutcome(StrEnum):
    """How an item was resolved.

        FR-WORK-04 (MUST): Every resolution records a structured outcome,
        not free text. Outcomes are the training signal.

    Free text is unusable for the thing resolutions are *for*. "Fixed it"
    and "corrected the member id" and "pt had new insurance" all describe
    the same outcome and no query can group them, so the feedback loop M4.9
    depends on -- preventable denial becomes a candidate rule -- has nothing
    to learn from. The note field still exists for what the operator wants
    to say; it sits beside the outcome rather than instead of it.
    """

    CORRECTED_AND_RESUBMITTED = "CORRECTED_AND_RESUBMITTED"
    APPEALED = "APPEALED"
    WRITTEN_OFF = "WRITTEN_OFF"
    BILLED_TO_PATIENT = "BILLED_TO_PATIENT"
    BILLED_TO_SECONDARY = "BILLED_TO_SECONDARY"
    PAYER_ERROR_CONFIRMED = "PAYER_ERROR_CONFIRMED"
    NO_ACTION_NEEDED = "NO_ACTION_NEEDED"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class WorkTransition:
    source: WorkItemState
    target: WorkItemState
    reason: str

    @property
    def key(self) -> tuple[WorkItemState, WorkItemState]:
        return (self.source, self.target)


TRANSITIONS: Final[tuple[WorkTransition, ...]] = (
    WorkTransition(WorkItemState.CREATED, WorkItemState.QUEUED, "priority scored"),
    WorkTransition(WorkItemState.QUEUED, WorkItemState.ASSIGNED, "operator claimed it"),
    WorkTransition(
        WorkItemState.QUEUED, WorkItemState.AUTO_RESOLVED, "system resolved before a human saw it"
    ),
    WorkTransition(WorkItemState.ASSIGNED, WorkItemState.IN_PROGRESS, "operator opened it"),
    WorkTransition(WorkItemState.IN_PROGRESS, WorkItemState.RESOLVED, "outcome recorded"),
    #: FR-WORK-02. The same edge serves a deliberate release and a lock that
    #: expired, because from the queue's point of view they are the same
    #: event: nobody is holding this any more.
    WorkTransition(WorkItemState.IN_PROGRESS, WorkItemState.QUEUED, "released or timed out"),
    WorkTransition(WorkItemState.IN_PROGRESS, WorkItemState.ESCALATED, "needs a supervisor"),
    WorkTransition(WorkItemState.ESCALATED, WorkItemState.ASSIGNED, "reassigned"),
    WorkTransition(WorkItemState.ESCALATED, WorkItemState.RESOLVED, "outcome recorded"),
    WorkTransition(WorkItemState.RESOLVED, WorkItemState.REOPENED, "outcome reversed"),
    WorkTransition(WorkItemState.REOPENED, WorkItemState.QUEUED, "returned to the queue"),
    #: An assignment that expires before the operator ever opened the item.
    #: SRS 7.7 draws the release only from `InProgress`, but an operator who
    #: claims an item and goes home without opening it leaves it locked
    #: forever otherwise -- which is the exact failure FR-WORK-02 exists to
    #: prevent, arriving one state earlier than the diagram anticipated.
    WorkTransition(WorkItemState.ASSIGNED, WorkItemState.QUEUED, "released or timed out"),
)

#: The one state an item never leaves. `Resolved` is deliberately absent.
FINAL_STATES: Final[frozenset[WorkItemState]] = frozenset({WorkItemState.AUTO_RESOLVED})

#: States in which a named operator is holding the item (FR-WORK-01), and
#: therefore the states a lock can expire from.
HELD_STATES: Final[frozenset[WorkItemState]] = frozenset(
    {WorkItemState.ASSIGNED, WorkItemState.IN_PROGRESS}
)

#: States an item sits in waiting for someone.
OPEN_STATES: Final[frozenset[WorkItemState]] = frozenset(
    {
        WorkItemState.CREATED,
        WorkItemState.QUEUED,
        WorkItemState.ASSIGNED,
        WorkItemState.IN_PROGRESS,
        WorkItemState.ESCALATED,
        WorkItemState.REOPENED,
    }
)

_BY_SOURCE: Final[dict[WorkItemState, tuple[WorkTransition, ...]]] = {
    state: tuple(t for t in TRANSITIONS if t.source is state) for state in WorkItemState
}
_BY_KEY: Final[dict[tuple[WorkItemState, WorkItemState], WorkTransition]] = {
    t.key: t for t in TRANSITIONS
}


def transitions_from(state: WorkItemState) -> tuple[WorkTransition, ...]:
    return _BY_SOURCE[state]


def is_final(state: WorkItemState) -> bool:
    return state in FINAL_STATES


def is_held(state: WorkItemState) -> bool:
    """Is a named operator holding this item right now?"""
    return state in HELD_STATES


def check(source: WorkItemState, target: WorkItemState) -> WorkTransition:
    """Validate a transition, or refuse it by name."""
    transition = _BY_KEY.get((source, target))
    if transition is None:
        raise StateTransitionError(
            source=str(source),
            target=str(target),
            reason="no such work item transition is defined",
            allowed=[str(t.target) for t in transitions_from(source)],
        )
    return transition
