"""PD-2: an audit event is written in the same transaction as the change it records.

The failure this prevents is an audit log that describes things that never
happened. If a claim update rolls back but its audit row was committed on its own,
every later reconciliation between the log and the data is wrong, and nobody can
tell which entries are real.

There is no claim table yet (that is M1), so a scratch table stands in for "the
change". What is under test is not the scratch table but the coupling: the change,
its audit event and its decision record live or die together, through the same
`session_scope()` the application uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, func, select, text

from rcm.platform.audit import (
    ActorType,
    AuditEvent,
    DecisionRecord,
    DecisionType,
    record_audit_event,
    record_decision,
)
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.integration

PROBE_TABLE = "m0_coupling_probe"


@pytest.fixture
def probe_table(owner_engine: Engine, app_engine: Engine) -> Iterator[str]:
    """A throwaway table owned by the migrator, writable by the app role."""
    del app_engine  # requested so an unreachable database fails before setup
    reset_engines()
    with owner_engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {PROBE_TABLE}"))
        conn.execute(text(f"CREATE TABLE {PROBE_TABLE} (entity_id uuid PRIMARY KEY)"))
        conn.execute(text(f"GRANT SELECT, INSERT ON {PROBE_TABLE} TO rcm_app"))
    yield PROBE_TABLE
    with owner_engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {PROBE_TABLE}"))


@pytest.fixture
def entity_id(owner_engine: Engine) -> Iterator[uuid.UUID]:
    """A fresh id per test, with committed audit rows removed afterwards.

    Cleanup runs as the owner because the application role cannot delete audit
    rows -- which is the property the neighbouring security test proves.
    """
    value = uuid.uuid4()
    yield value
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_event WHERE entity_id = :e"), {"e": value})
        conn.execute(text("DELETE FROM decision_record WHERE entity_id = :e"), {"e": value})


def _write_change_with_audit(session: object, table: str, entity_id: uuid.UUID) -> None:
    session.execute(text(f"INSERT INTO {table} (entity_id) VALUES (:e)"), {"e": entity_id})  # type: ignore[attr-defined]
    record_audit_event(
        session,  # type: ignore[arg-type]
        actor_type=ActorType.SYSTEM,
        actor_id="test:pd-2",
        action="probe.created",
        entity_type="probe",
        entity_id=entity_id,
        after_state={"entity_id": str(entity_id)},
    )
    record_decision(
        session,  # type: ignore[arg-type]
        entity_type="probe",
        entity_id=entity_id,
        decision_type=DecisionType.SCRUB,
        decided_by="rules_engine",
        logic_version="test-v0",
        input_snapshot={"entity_id": str(entity_id)},
        output={"result": "pass"},
    )


def _counts(table: str, entity_id: uuid.UUID) -> tuple[int, int, int]:
    """(change rows, audit rows, decision rows) for `entity_id`, read as the app role."""
    with session_scope() as session:
        change = session.execute(
            text(f"SELECT count(*) FROM {table} WHERE entity_id = :e"), {"e": entity_id}
        ).scalar_one()
        audit = session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.entity_id == entity_id)
        )
        decision = session.scalar(
            select(func.count())
            .select_from(DecisionRecord)
            .where(DecisionRecord.entity_id == entity_id)
        )
    return int(change), int(audit or 0), int(decision or 0)


# ---------------------------------------------------------------------------
# The named invariant test (CLAUDE.md section 7.2)
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_audit_written_in_same_transaction(probe_table: str, entity_id: uuid.UUID) -> None:
    """A failure after the writes removes the change *and* its audit trail.

    This is the realistic shape: the handler writes the change, records the audit
    event, then something later in the same request raises.
    """

    class DownstreamFailureError(Exception):
        pass

    with pytest.raises(DownstreamFailureError), session_scope() as session:
        _write_change_with_audit(session, probe_table, entity_id)
        session.flush()  # the rows really reached the database inside the transaction
        raise DownstreamFailureError

    assert _counts(probe_table, entity_id) == (0, 0, 0), (
        "a rolled-back change left audit or decision rows behind -- "
        "the log now records something that never happened"
    )


def test_committed_change_keeps_its_audit_trail(probe_table: str, entity_id: uuid.UUID) -> None:
    """The other half: success persists all three. Without this, a scope that
    silently wrote nothing would pass the test above."""
    with session_scope() as session:
        _write_change_with_audit(session, probe_table, entity_id)

    assert _counts(probe_table, entity_id) == (1, 1, 1)
