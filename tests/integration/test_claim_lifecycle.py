"""Moving claims through the machine, durably (M3.2).

    FR-BE-10 (MUST): Transitions occur inside a database transaction with
    row-level locking. Concurrent transitions must not interleave.

The concurrency test is the one that matters and the one that is easy to
write so it proves nothing. Two sessions that merely run one after another
would pass without any lock at all, so the test below holds the first
transaction open, starts the second, and asserts it is still blocked before
letting the first commit. If the lock were removed the second would sail
through and the assertion would fail.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from rcm.domain.claim import Claim
from rcm.domain.claim_lifecycle import (
    claims_needing_action,
    current_state,
    history,
    transition,
)
from rcm.domain.claim_state import ClaimState, Guard
from rcm.domain.claim_state_log import ClaimStateTransition
from rcm.ingestion.claim_loader import load_claim_file
from rcm.platform.audit import ActorType, AuditEvent
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ResourceNotFoundError, StateTransitionError
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.integration

CLIENT = uuid.UUID("3a3a3a3a-0000-4000-8000-00000000003a")
OTHER_CLIENT = uuid.UUID("3b3b3b3b-0000-4000-8000-00000000003b")
SOON = date(2026, 12, 1)

TABLES = (
    "claim_state_transition, claim_acknowledgement, claim_line, claim, "
    "remittance_adjustment, remittance_service_line, claim_payment, "
    "provider_adjustment, remittance, raw_file, payer_alias, payer, payer_match_candidate"
)


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def loaded_claim(archive_root: Path) -> uuid.UUID:
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim("CLM-LIFECYCLE", charge=str(Decimal("200.00")))),
            client_id=CLIENT,
            original_filename="claim.edi",
            archive_root=archive_root,
        )
    with session_scope() as session:
        found = session.scalar(select(Claim).where(Claim.client_id == CLIENT))
        assert found is not None
        return found.claim_id


def move(claim_id: uuid.UUID, target: ClaimState, **kwargs: object):
    with session_scope() as session:
        return transition(
            session,
            claim_id=claim_id,
            client_id=CLIENT,
            target=target,
            actor_type=ActorType.SYSTEM,
            actor_id="system:test",
            **kwargs,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# The basics
# ---------------------------------------------------------------------------


def test_a_loaded_claim_starts_registered(loaded_claim: uuid.UUID) -> None:
    """Every claim already in the table predates the state machine and is
    given `Registered` by the migration's server default, which is truthful:
    it is where a claim starts and nothing has moved it since."""
    with session_scope() as session:
        assert current_state(session, claim_id=loaded_claim, client_id=CLIENT) is (
            ClaimState.REGISTERED
        )


def test_a_defined_transition_moves_the_claim(loaded_claim: uuid.UUID) -> None:
    result = move(loaded_claim, ClaimState.ELIGIBILITY_VERIFIED, next_action_due=SOON)

    assert result.source is ClaimState.REGISTERED
    assert result.target is ClaimState.ELIGIBILITY_VERIFIED
    assert result.reason == "eligibility checked"
    with session_scope() as session:
        assert current_state(session, claim_id=loaded_claim, client_id=CLIENT) is (
            ClaimState.ELIGIBILITY_VERIFIED
        )


@pytest.mark.invariant
def test_an_undefined_transition_is_refused_and_changes_nothing(loaded_claim: uuid.UUID) -> None:
    """BR-STATE-02. The refusal must also leave no trace: a rejected
    transition that still wrote a history row would make the log disagree
    with the claim."""
    with pytest.raises(StateTransitionError), session_scope() as session:
        transition(
            session,
            claim_id=loaded_claim,
            client_id=CLIENT,
            target=ClaimState.PAID,
            actor_type=ActorType.SYSTEM,
            actor_id="system:test",
            next_action_due=SOON,
        )

    with session_scope() as session:
        assert current_state(session, claim_id=loaded_claim, client_id=CLIENT) is (
            ClaimState.REGISTERED
        )
        assert session.scalar(select(ClaimStateTransition).limit(1)) is None


# ---------------------------------------------------------------------------
# BR-STATE-01 and BR-STATE-04
# ---------------------------------------------------------------------------


def test_every_transition_is_logged_with_actor_and_reason(loaded_claim: uuid.UUID) -> None:
    """BR-STATE-01: timestamp, actor, reason."""
    move(loaded_claim, ClaimState.ELIGIBILITY_VERIFIED, next_action_due=SOON)
    move(
        loaded_claim,
        ClaimState.SERVICE_RENDERED,
        facts={str(Guard.AUTH_NOT_REQUIRED): True},
        next_action_due=SOON,
    )

    with session_scope() as session:
        rows = history(session, claim_id=loaded_claim, client_id=CLIENT)

    assert [(r.source_state, r.target_state) for r in rows] == [
        ("Registered", "EligibilityVerified"),
        ("EligibilityVerified", "ServiceRendered"),
    ]
    assert rows[1].reason == "no authorisation needed"
    assert rows[1].guard == "auth_not_required"
    assert all(r.actor_id == "system:test" and r.occurred_at for r in rows)


@pytest.mark.invariant
def test_claim_never_rests_without_next_action(loaded_claim: uuid.UUID) -> None:
    """BR-STATE-04, enforced at the moment the claim enters the state.

    Any later moment is too late: the claim is already sitting there, and
    the only thing that would surface it is the query that is looking for a
    date it does not have.
    """
    with pytest.raises(StateTransitionError) as raised, session_scope() as session:
        transition(
            session,
            claim_id=loaded_claim,
            client_id=CLIENT,
            target=ClaimState.ELIGIBILITY_VERIFIED,
            actor_type=ActorType.SYSTEM,
            actor_id="system:test",
        )
    assert "BR-STATE-04" in raised.value.context["reason"]


def test_a_resting_state_needs_no_next_action(loaded_claim: uuid.UUID) -> None:
    """And is given none, so a closed claim does not reappear on a queue
    forever because of a date it carried in."""
    for target, facts in (
        (ClaimState.ELIGIBILITY_VERIFIED, None),
        (ClaimState.AUTH_PENDING, {str(Guard.AUTH_REQUIRED): True}),
        (ClaimState.AUTH_DENIED, None),
    ):
        move(loaded_claim, target, facts=facts, next_action_due=SOON)

    result = move(loaded_claim, ClaimState.CANCELLED)
    assert result.next_action_due is None

    with session_scope() as session:
        found = session.get(Claim, loaded_claim)
        assert found is not None
        assert found.next_action_due is None


def test_claims_needing_action_finds_the_unscheduled_ones(loaded_claim: uuid.UUID) -> None:
    """The half of BR-STATE-04 that matters: a claim with no due date in a
    state that requires one has fallen out of the process, and nothing else
    will ever surface it."""
    move(loaded_claim, ClaimState.ELIGIBILITY_VERIFIED, next_action_due=SOON)

    # Simulate the row having lost its date -- a backfill, a hand edit, a
    # bug in a future caller that bypassed this module.
    with session_scope() as session:
        found = session.get(Claim, loaded_claim)
        assert found is not None
        found.next_action_due = None

    with session_scope() as session:
        due = claims_needing_action(session, client_id=CLIENT, as_of=date(2026, 1, 1))

    assert len(due) == 1
    assert due[0]["unscheduled"] is True
    assert due[0]["state"] == "EligibilityVerified"


def test_a_resting_claim_is_not_chased(loaded_claim: uuid.UUID) -> None:
    for target, facts in (
        (ClaimState.ELIGIBILITY_VERIFIED, None),
        (ClaimState.AUTH_PENDING, {str(Guard.AUTH_REQUIRED): True}),
        (ClaimState.AUTH_DENIED, None),
    ):
        move(loaded_claim, target, facts=facts, next_action_due=SOON)
    move(loaded_claim, ClaimState.CANCELLED)

    with session_scope() as session:
        assert claims_needing_action(session, client_id=CLIENT, as_of=date(2030, 1, 1)) == []


# ---------------------------------------------------------------------------
# PD-2 / FR-BE-12
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_audit_written_in_same_transaction(loaded_claim: uuid.UUID) -> None:
    """PD-2. The state change and its audit row are one write or none.

    The rollback half is the real test: a transition that was abandoned
    must leave no audit event claiming it happened.
    """

    # Scoped to this claim, not to the action. `audit_event` is append-only
    # by design (DR-06) and deliberately not truncated between tests, so a
    # query by action alone sees every transition every other test made.
    def events_for_this_claim(session) -> list[AuditEvent]:
        return list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "claim.transitioned",
                    AuditEvent.entity_id == loaded_claim,
                )
            ).all()
        )

    with session_scope() as session:
        assert events_for_this_claim(session) == []

    move(loaded_claim, ClaimState.ELIGIBILITY_VERIFIED, next_action_due=SOON)

    with session_scope() as session:
        events = events_for_this_claim(session)
    assert len(events) == 1


def test_a_rolled_back_transition_leaves_no_audit_event(loaded_claim: uuid.UUID) -> None:
    from rcm.platform.db import session_factory

    maker = session_factory()
    session = maker()
    try:
        transition(
            session,
            claim_id=loaded_claim,
            client_id=CLIENT,
            target=ClaimState.ELIGIBILITY_VERIFIED,
            actor_type=ActorType.SYSTEM,
            actor_id="system:test",
            next_action_due=SOON,
        )
        session.rollback()
    finally:
        session.close()

    with session_scope() as check_session:
        assert (
            current_state(check_session, claim_id=loaded_claim, client_id=CLIENT)
            is ClaimState.REGISTERED
        )
        assert check_session.scalar(select(ClaimStateTransition).limit(1)) is None
        assert (
            check_session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "claim.transitioned",
                    AuditEvent.entity_id == loaded_claim,
                )
            )
            is None
        )


# ---------------------------------------------------------------------------
# PD-20
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_another_clients_claim_cannot_be_moved(loaded_claim: uuid.UUID) -> None:
    """PD-20. The client is part of the lookup, not a filter applied after,
    so a claim id alone is never enough."""
    with pytest.raises(ResourceNotFoundError), session_scope() as session:
        transition(
            session,
            claim_id=loaded_claim,
            client_id=OTHER_CLIENT,
            target=ClaimState.ELIGIBILITY_VERIFIED,
            actor_type=ActorType.SYSTEM,
            actor_id="system:intruder",
            next_action_due=SOON,
        )

    with session_scope() as session:
        assert (
            current_state(session, claim_id=loaded_claim, client_id=CLIENT) is ClaimState.REGISTERED
        )


# ---------------------------------------------------------------------------
# FR-BE-10
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_concurrent_transitions_do_not_interleave(loaded_claim: uuid.UUID) -> None:
    """FR-BE-10, proved by holding the lock rather than by running twice.

    Two operators act on one claim: one writes it off, the other appeals.
    Both read `Denied` and both decide. Without `FOR UPDATE` the second
    write lands on a claim that is no longer in the state it was validated
    against, and the loser's transition is recorded as though it had applied
    to a state the winner already left.

    The test starts the second transaction while the first is still open and
    asserts it is *still blocked*. Remove the lock and that assertion fails
    immediately -- which is what makes this a test of the lock rather than
    of two sequential writes.
    """
    for target, facts in (
        (ClaimState.ELIGIBILITY_VERIFIED, None),
        (ClaimState.SERVICE_RENDERED, {str(Guard.AUTH_NOT_REQUIRED): True}),
        (ClaimState.CODED, None),
        (ClaimState.CHARGED, None),
        (ClaimState.SCRUBBING, None),
        (ClaimState.READY_TO_SUBMIT, None),
        (ClaimState.SUBMITTED, None),
        (ClaimState.IN_ADJUDICATION, None),
        (ClaimState.DENIED, None),
    ):
        move(loaded_claim, target, facts=facts, next_action_due=SOON)

    from rcm.platform.db import session_factory

    maker = session_factory()
    first_has_lock = threading.Event()
    second_finished = threading.Event()
    outcome: dict[str, object] = {}

    holder = maker()
    transition(
        holder,
        claim_id=loaded_claim,
        client_id=CLIENT,
        target=ClaimState.WRITTEN_OFF,
        actor_type=ActorType.USER,
        actor_id="operator:a",
        next_action_due=SOON,
    )
    first_has_lock.set()

    def contender() -> None:
        session = maker()
        try:
            transition(
                session,
                claim_id=loaded_claim,
                client_id=CLIENT,
                target=ClaimState.APPEALING,
                actor_type=ActorType.USER,
                actor_id="operator:b",
                next_action_due=SOON,
            )
            session.commit()
            outcome["result"] = "applied"
        except StateTransitionError as exc:
            outcome["result"] = "refused"
            outcome["reason"] = exc.context.get("reason")
            session.rollback()
        finally:
            session.close()
            second_finished.set()

    thread = threading.Thread(target=contender, daemon=True)
    thread.start()

    # The contender must still be waiting on the row lock. If this clears,
    # the second transaction read the claim while the first held it.
    assert not second_finished.wait(timeout=2.0), (
        "the second transition completed while the first still held the row lock"
    )

    holder.commit()
    holder.close()
    thread.join(timeout=15)

    # Having waited, the contender re-read the claim and found it written
    # off -- from which `Appealing` is not a defined transition. The lock
    # did not merely serialise the writes, it made the second one correct.
    assert outcome["result"] == "refused"
    assert outcome["reason"] == "no such transition is defined"

    with session_scope() as session:
        assert (
            current_state(session, claim_id=loaded_claim, client_id=CLIENT)
            is ClaimState.WRITTEN_OFF
        )
        assert [r.target_state for r in history(session, claim_id=loaded_claim, client_id=CLIENT)][
            -1
        ] == "WrittenOff"
