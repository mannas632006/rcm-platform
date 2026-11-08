"""The claim state machine, declared as data (M3.1).

    FR-BE-09 (MUST): Valid transitions are declared as data, not scattered
    through `if` statements. Undefined transitions are rejected
    (DOC-02 BR-STATE-02).

DOC-02 section 7.1 draws twenty-five states and thirty-six transitions. The
reason they live in a table rather than in code is not elegance: a claim's
life is the product's subject matter, and a reader who wants to know whether
a denied claim can be appealed should be able to find the answer by reading
one list rather than by tracing branches through a service layer. A table can
also be enumerated, which is what lets `tests/unit/test_claim_state.py`
assert that *every* defined transition works and that nothing else does.

Three distinctions this module is careful about, because collapsing any of
them loses something the business rules depend on.

**`Closed` is not final.**

    BR-STATE-03 (MUST): `Closed` is not final -- reversals and recoupments
    can reopen a claim months later.

A claim closed in March and recouped in September was closed, correctly, in
March. So `Closed` has an outgoing transition and `is_final` is false for it.
The only state a claim never leaves is `Cancelled`.

**Resting is not the same as final.**

    BR-STATE-04 (MUST): No claim may remain in a non-terminal state without
    a scheduled next action.

A closed claim needs no next action; a claim sitting in `AuthPending` very
much does, and a claim that has been waiting there for six weeks because
nobody scheduled a follow-up is the single most common way money is lost
quietly. `requires_next_action` is therefore its own property, true for
every state except the two a claim can rest in. It is deliberately *not*
`not is_final`, because `Closed` is not final and still requires nothing.

**A guard is a condition, not a branch.**

Some transitions apply only in particular circumstances -- a claim goes to
`AuthPending` only when authorisation is required, and to `SecondaryBilling`
only when secondary coverage exists. Those conditions are named here and
checked by the caller supplying facts, so that the reason a transition was
refused is a sentence rather than a silent fall-through to the other branch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from rcm.platform.errors import StateTransitionError


class ClaimState(StrEnum):
    """Every state a claim can be in (DOC-02 section 7.1)."""

    # --- before the service ------------------------------------------------
    REGISTERED = "Registered"
    ELIGIBILITY_VERIFIED = "EligibilityVerified"
    AUTH_PENDING = "AuthPending"
    AUTH_APPROVED = "AuthApproved"
    AUTH_DENIED = "AuthDenied"
    # --- the service and the bill ------------------------------------------
    SERVICE_RENDERED = "ServiceRendered"
    CODED = "Coded"
    CHARGED = "Charged"
    SCRUBBING = "Scrubbing"
    SCRUB_FAILED = "ScrubFailed"
    READY_TO_SUBMIT = "ReadyToSubmit"
    # --- with the payer -----------------------------------------------------
    SUBMITTED = "Submitted"
    #: PD-4: returned by the front end, never adjudicated. Not a denial.
    REJECTED = "Rejected"
    IN_ADJUDICATION = "InAdjudication"
    PAID = "Paid"
    PARTIALLY_PAID = "PartiallyPaid"
    #: PD-4: adjudicated and refused. Not a rejection.
    DENIED = "Denied"
    # --- working the outcome ------------------------------------------------
    APPEALING = "Appealing"
    CORRECTING = "Correcting"
    WRITTEN_OFF = "WrittenOff"
    SECONDARY_BILLING = "SecondaryBilling"
    PATIENT_BALANCE = "PatientBalance"
    # --- at rest -------------------------------------------------------------
    CLOSED = "Closed"
    #: BR-STATE-03: what `Closed` becomes when money moves backwards.
    REOPENED = "Reopened"
    CANCELLED = "Cancelled"
    #: ADR-0006: a submission superseded by a corrected one (REF*F8). The
    #: claim carries on in the new submission; this one is finished.
    REPLACED = "Replaced"


class Guard(StrEnum):
    """A condition a transition depends on.

    Named rather than expressed as a lambda so that a refusal can say which
    condition was not met, and so that the set of facts the machine consults
    is enumerable.
    """

    AUTH_REQUIRED = "auth_required"
    AUTH_NOT_REQUIRED = "auth_not_required"
    SECONDARY_COVERAGE_EXISTS = "secondary_coverage_exists"
    NO_SECONDARY_COVERAGE = "no_secondary_coverage"
    #: The denied lines carry a patient-responsibility (PR) amount. Only
    #: that amount may follow a denial to the patient (ADR-0016, PD-19).
    PATIENT_SHARE_DENIED = "patient_share_denied"


#: Guards that are each other's negation. A context satisfying one must not
#: satisfy the other, which is what makes the pair of transitions out of a
#: state exhaustive and mutually exclusive rather than merely adjacent.
OPPOSED_GUARDS: Final[tuple[tuple[Guard, Guard], ...]] = (
    (Guard.AUTH_REQUIRED, Guard.AUTH_NOT_REQUIRED),
    (Guard.SECONDARY_COVERAGE_EXISTS, Guard.NO_SECONDARY_COVERAGE),
)


@dataclass(frozen=True, slots=True)
class Transition:
    """One edge of the machine."""

    source: ClaimState
    target: ClaimState
    #: Why this edge exists, in the domain's words. Surfaced in the audit
    #: record, so an operator reading a claim's history sees "999/277CA
    #: rejected" rather than "SUBMITTED -> REJECTED".
    reason: str
    guard: Guard | None = None

    @property
    def key(self) -> tuple[ClaimState, ClaimState]:
        return (self.source, self.target)


#: Every transition DOC-02 section 7.1 defines. Nothing else is permitted.
TRANSITIONS: Final[tuple[Transition, ...]] = (
    # --- before the service ------------------------------------------------
    Transition(ClaimState.REGISTERED, ClaimState.ELIGIBILITY_VERIFIED, "eligibility checked"),
    Transition(
        ClaimState.ELIGIBILITY_VERIFIED,
        ClaimState.AUTH_PENDING,
        "authorisation required for this service",
        Guard.AUTH_REQUIRED,
    ),
    Transition(
        ClaimState.ELIGIBILITY_VERIFIED,
        ClaimState.SERVICE_RENDERED,
        "no authorisation needed",
        Guard.AUTH_NOT_REQUIRED,
    ),
    Transition(ClaimState.AUTH_PENDING, ClaimState.AUTH_APPROVED, "payer approved the request"),
    Transition(ClaimState.AUTH_PENDING, ClaimState.AUTH_DENIED, "payer refused the request"),
    Transition(ClaimState.AUTH_APPROVED, ClaimState.SERVICE_RENDERED, "service delivered"),
    Transition(
        ClaimState.AUTH_DENIED,
        ClaimState.CANCELLED,
        "no authorisation, so the service is not billed",
    ),
    # --- the service and the bill ------------------------------------------
    Transition(ClaimState.SERVICE_RENDERED, ClaimState.CODED, "coder assigned codes"),
    Transition(ClaimState.CODED, ClaimState.CHARGED, "charges entered"),
    Transition(ClaimState.CHARGED, ClaimState.SCRUBBING, "sent to the scrubber"),
    Transition(ClaimState.SCRUBBING, ClaimState.SCRUB_FAILED, "scrubber raised an edit"),
    Transition(ClaimState.SCRUB_FAILED, ClaimState.CHARGED, "corrected"),
    Transition(ClaimState.SCRUBBING, ClaimState.READY_TO_SUBMIT, "scrubber found nothing"),
    Transition(ClaimState.READY_TO_SUBMIT, ClaimState.SUBMITTED, "sent to the clearinghouse"),
    # --- with the payer -----------------------------------------------------
    #: PD-4. A rejection and an acceptance are the two things a front end can
    #: say, and they lead to different halves of the machine: a rejection
    #: goes back to `Charged` to be fixed and resent, an acceptance goes
    #: forward into adjudication. BR-M8-02: only a 277CA acceptance counts.
    Transition(ClaimState.SUBMITTED, ClaimState.REJECTED, "999 or 277CA rejected the claim"),
    Transition(ClaimState.REJECTED, ClaimState.CHARGED, "corrected for resubmission"),
    Transition(ClaimState.SUBMITTED, ClaimState.IN_ADJUDICATION, "277CA accepted the claim"),
    Transition(ClaimState.IN_ADJUDICATION, ClaimState.PAID, "835 paid in full"),
    Transition(ClaimState.IN_ADJUDICATION, ClaimState.PARTIALLY_PAID, "835 paid in part"),
    Transition(ClaimState.IN_ADJUDICATION, ClaimState.DENIED, "835 refused payment"),
    #: A status check that tells us nothing new still happened, and the claim
    #: still needs a next action date (BR-STATE-04). A self-transition is how
    #: that gets recorded rather than silently skipped.
    Transition(
        ClaimState.IN_ADJUDICATION, ClaimState.IN_ADJUDICATION, "276/277 status check, no change"
    ),
    # --- working the outcome ------------------------------------------------
    Transition(ClaimState.DENIED, ClaimState.APPEALING, "appeal submitted"),
    Transition(ClaimState.DENIED, ClaimState.CORRECTING, "denial is correctable"),
    Transition(ClaimState.DENIED, ClaimState.WRITTEN_OFF, "not worth pursuing"),
    #: ADR-0016 (owner, 2026-11-06): the payer's refusal is accepted and the
    #: patient is billed their PR share; the CO amount is never theirs.
    Transition(
        ClaimState.DENIED,
        ClaimState.PATIENT_BALANCE,
        "denied amount is the patient's",
        Guard.PATIENT_SHARE_DENIED,
    ),
    Transition(ClaimState.CORRECTING, ClaimState.CHARGED, "corrected for resubmission"),
    Transition(ClaimState.APPEALING, ClaimState.PAID, "appeal succeeded"),
    Transition(ClaimState.APPEALING, ClaimState.DENIED, "appeal failed"),
    Transition(
        ClaimState.PAID,
        ClaimState.SECONDARY_BILLING,
        "secondary coverage to bill",
        Guard.SECONDARY_COVERAGE_EXISTS,
    ),
    Transition(
        ClaimState.PARTIALLY_PAID,
        ClaimState.SECONDARY_BILLING,
        "secondary coverage to bill",
        Guard.SECONDARY_COVERAGE_EXISTS,
    ),
    Transition(ClaimState.SECONDARY_BILLING, ClaimState.CHARGED, "billed to the secondary payer"),
    Transition(
        ClaimState.PAID,
        ClaimState.PATIENT_BALANCE,
        "balance is the patient's",
        Guard.NO_SECONDARY_COVERAGE,
    ),
    Transition(
        ClaimState.PARTIALLY_PAID,
        ClaimState.PATIENT_BALANCE,
        "balance is the patient's",
        Guard.NO_SECONDARY_COVERAGE,
    ),
    Transition(ClaimState.PATIENT_BALANCE, ClaimState.CLOSED, "patient balance settled"),
    Transition(ClaimState.WRITTEN_OFF, ClaimState.CLOSED, "written off"),
    # --- and back again -----------------------------------------------------
    #: BR-STATE-03. The transition that makes `Closed` not final.
    Transition(ClaimState.CLOSED, ClaimState.REOPENED, "reversal or recoupment"),
    Transition(ClaimState.REOPENED, ClaimState.IN_ADJUDICATION, "re-adjudicated"),
    # --- superseded (ADR-0006) ------------------------------------------------
    #: BR-DATA-03 keeps one claim across its submissions; each submission row
    #: still needs an end. A replacement can follow anything the payer saw.
    *(
        Transition(source, ClaimState.REPLACED, "superseded by a corrected submission")
        for source in (
            ClaimState.SUBMITTED,
            ClaimState.REJECTED,
            ClaimState.IN_ADJUDICATION,
            ClaimState.DENIED,
            ClaimState.PARTIALLY_PAID,
            ClaimState.PAID,
            ClaimState.CORRECTING,
        )
    ),
)

#: The one state a claim never leaves. `Closed` is deliberately absent
#: (BR-STATE-03).
FINAL_STATES: Final[frozenset[ClaimState]] = frozenset({ClaimState.CANCELLED, ClaimState.REPLACED})

#: States a claim may sit in without a scheduled next action (BR-STATE-04).
#:
#: `Closed` is here and is not in `FINAL_STATES`: a closed claim needs no
#: follow-up, and can still be reopened months later when money moves
#: backwards. Conflating the two would either demand a next action date for
#: every closed claim forever, or let a reopened one disappear.
RESTING_STATES: Final[frozenset[ClaimState]] = frozenset(
    {ClaimState.CLOSED, ClaimState.CANCELLED, ClaimState.REPLACED}
)

#: Where a claim begins.
INITIAL_STATE: Final = ClaimState.REGISTERED

_BY_SOURCE: Final[dict[ClaimState, tuple[Transition, ...]]] = {
    state: tuple(t for t in TRANSITIONS if t.source is state) for state in ClaimState
}
_BY_KEY: Final[dict[tuple[ClaimState, ClaimState], Transition]] = {t.key: t for t in TRANSITIONS}


def transitions_from(state: ClaimState) -> tuple[Transition, ...]:
    """Every edge leaving `state`, in declaration order."""
    return _BY_SOURCE[state]


def is_final(state: ClaimState) -> bool:
    """Can the claim ever leave this state?

    False for `Closed`, which is the whole of BR-STATE-03.
    """
    return state in FINAL_STATES


def requires_next_action(state: ClaimState) -> bool:
    """BR-STATE-04: must a claim in this state have a scheduled next action?

    True for every state a claim is passing through, false for the two it
    can rest in. Note this is *not* `not is_final(state)`: a closed claim
    needs no follow-up and is still not final.
    """
    return state not in RESTING_STATES


def find(source: ClaimState, target: ClaimState) -> Transition | None:
    """The declared transition between two states, if there is one."""
    return _BY_KEY.get((source, target))


def check(
    source: ClaimState,
    target: ClaimState,
    *,
    facts: Mapping[str, bool] | None = None,
) -> Transition:
    """Validate a transition, or refuse it by name (BR-STATE-02).

    `facts` answers the guards: `{"auth_required": True}`. A transition with
    a guard is refused unless its fact is present and true -- absence is not
    permission (PD-18). The alternative, treating a missing fact as false,
    would quietly route every claim down the no-authorisation branch of a
    machine that was never asked the question.
    """
    transition = find(source, target)
    if transition is None:
        raise StateTransitionError(
            source=str(source),
            target=str(target),
            reason="no such transition is defined",
            allowed=[str(t.target) for t in transitions_from(source)],
        )

    if transition.guard is not None:
        answers = facts or {}
        if not answers.get(str(transition.guard), False):
            raise StateTransitionError(
                source=str(source),
                target=str(target),
                reason=f"guard not satisfied: {transition.guard}",
                guard=str(transition.guard),
            )

    return transition
