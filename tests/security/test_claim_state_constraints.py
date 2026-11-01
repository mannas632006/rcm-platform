"""The database refuses a state the machine has never heard of (M3.1, M3.2).

`rcm.domain.claim_state` refuses an undefined *transition*. This file is
about the other half: an undefined *state*, which arrives by a route the
machine never sees -- a backfill, a repair script, somebody with psql and a
deadline.

Every assertion goes through the **application** role, because that is the
identity the platform runs as. A constraint only the owner observes is not a
control.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from rcm.domain.claim_state import ClaimState

pytestmark = pytest.mark.security

CLIENT = uuid.UUID("3c3c3c3c-0000-4000-8000-00000000003c")


@pytest.fixture
def seeded(owner_engine: Engine) -> Iterator[dict[str, uuid.UUID]]:
    ids = {"raw_file": uuid.uuid4(), "claim": uuid.uuid4()}

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE claim_state_transition, claim_line, claim, raw_file, payer "
                    "RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    with owner_engine.begin() as conn:
        conn.execute(text("INSERT INTO payer (payer_id, name) VALUES ('P-STATE', 'A Payer')"))
        conn.execute(
            text(
                "INSERT INTO raw_file (raw_file_id, client_id, content_hash, byte_size, "
                "storage_path, original_filename, transaction_kind, received_at, status) "
                "VALUES (:id, :client, :hash, 1, 'p', 'f.edi', '837', now(), 'ARCHIVED')"
            ),
            {"id": ids["raw_file"], "client": CLIENT, "hash": uuid.uuid4().hex},
        )
        conn.execute(
            text(
                "INSERT INTO claim (claim_id, client_id, raw_file_id, submitter_claim_id, "
                "root_claim_id, payer_id, billing_npi, subscriber_member_id, total_charge) "
                "VALUES (:id, :client, :raw, 'CLM-STATE', :id, 'P-STATE', '1234567893', "
                "'M1', 100)"
            ),
            {"id": ids["claim"], "client": CLIENT, "raw": ids["raw_file"]},
        )
    yield ids
    wipe()


# ---------------------------------------------------------------------------
# The state list cannot drift
# ---------------------------------------------------------------------------


def test_the_migration_and_the_enum_agree(owner_engine: Engine) -> None:
    """The state list exists in two places -- the enum the code reads and
    the CHECK the database enforces -- and a list in two places drifts
    unless something compares them.

    Read from the live constraint rather than from the migration source, so
    this also catches a database that was migrated before a state was added
    and never caught up.
    """
    with owner_engine.begin() as conn:
        definition = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_claim_state_is_known'"
            )
        ).scalar_one()

    # Postgres renders each value with its cast -- `'Paid'::character
    # varying` -- so the quoted literals are pulled out rather than the
    # comma-separated fragments.
    in_database = set(re.findall(r"'([^']+)'", definition))
    in_code = {str(state) for state in ClaimState}

    assert in_database == in_code, {
        "only in the database": sorted(in_database - in_code),
        "only in the code": sorted(in_code - in_database),
    }


# ---------------------------------------------------------------------------
# The claim's state
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_claim_cannot_hold_an_unknown_state(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """A state nothing defines is a claim nothing can advance: every
    transition out of it is undefined, so it is stuck in a way BR-STATE-04
    cannot express and no queue will surface."""
    with pytest.raises(IntegrityError, match="state_is_known"), app_engine.begin() as conn:
        conn.execute(
            text("UPDATE claim SET state = :state WHERE claim_id = :id"),
            {"state": "Adjudicating", "id": seeded["claim"]},
        )


@pytest.mark.parametrize("state", ["paid", "PAID", "", "Pending"])
def test_near_misses_are_refused_too(
    app_engine: Engine, seeded: dict[str, uuid.UUID], state: str
) -> None:
    """Case matters. `paid` and `Paid` would be two states to every query
    that groups by it, and only one of them would ever be read."""
    with pytest.raises(IntegrityError, match="state_is_known"), app_engine.begin() as conn:
        conn.execute(
            text("UPDATE claim SET state = :state WHERE claim_id = :id"),
            {"state": state, "id": seeded["claim"]},
        )


def test_every_defined_state_is_accepted(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """The other direction: a constraint that refused a legitimate state
    would block the machine at exactly the transition it was meant to
    permit."""
    with app_engine.begin() as conn:
        for state in ClaimState:
            conn.execute(
                text("UPDATE claim SET state = :state WHERE claim_id = :id"),
                {"state": str(state), "id": seeded["claim"]},
            )


# ---------------------------------------------------------------------------
# The transition log
# ---------------------------------------------------------------------------


def _insert_transition(conn: object, claim_id: uuid.UUID, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "client": CLIENT,
        "claim": claim_id,
        "source": "Registered",
        "target": "EligibilityVerified",
        "reason": "eligibility checked",
        "actor_type": "SYSTEM",
        "actor_id": "system:test",
    }
    values.update(overrides)
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO claim_state_transition (transition_id, client_id, claim_id, "
            "source_state, target_state, reason, actor_type, actor_id) "
            "VALUES (:id, :client, :claim, :source, :target, :reason, :actor_type, :actor_id)"
        ),
        values,
    )


def test_a_well_formed_transition_is_accepted(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    with app_engine.begin() as conn:
        _insert_transition(conn, seeded["claim"])


@pytest.mark.invariant
def test_a_transition_must_state_a_reason(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """BR-STATE-01. A transition with no reason records that something
    happened and refuses to say what, which is the shape of an audit trail
    that cannot be audited."""
    with (
        pytest.raises(IntegrityError, match="transition_states_a_reason"),
        app_engine.begin() as conn,
    ):
        _insert_transition(conn, seeded["claim"], reason="")


def test_a_transition_must_name_its_actor(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """BR-STATE-01 again: human or system, and which one. "The system did
    it" is the answer nobody can follow up on."""
    with (
        pytest.raises(IntegrityError, match="transition_names_its_actor"),
        app_engine.begin() as conn,
    ):
        _insert_transition(conn, seeded["claim"], actor_id="")


def test_a_transition_must_name_both_states(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    for column, constraint in (
        ("source", "transition_names_its_source"),
        ("target", "transition_names_its_target"),
    ):
        with pytest.raises(IntegrityError, match=constraint), app_engine.begin() as conn:
            _insert_transition(conn, seeded["claim"], **{column: ""})


def test_a_self_transition_is_allowed(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """Deliberately permitted. A 276/277 status check that changes nothing
    still happened and still resets the next action date; forbidding it
    would mean the one event that proves a claim is being chased is the one
    event that cannot be recorded."""
    with app_engine.begin() as conn:
        _insert_transition(
            conn,
            seeded["claim"],
            source="InAdjudication",
            target="InAdjudication",
            reason="276/277 status check, no change",
        )


def test_a_transition_must_belong_to_a_claim(app_engine: Engine) -> None:
    """PD-10: every row traces to something. A transition for a claim that
    does not exist is a history nothing can be reconstructed from."""
    with pytest.raises(IntegrityError, match="claim_id"), app_engine.begin() as conn:
        _insert_transition(conn, uuid.uuid4())
