"""DR-06 / S-10 / PD-1 / PD-2: the audit tables are append-only for the app role.

The control is a database grant (migration 0002), so this test talks to real
Postgres as the real application role. On any other backend it would prove nothing.

Every mutation below is issued as **raw SQL**, deliberately. `rcm.platform.audit`
also has an ORM-level guard that refuses updates and deletes before they reach the
database. If these tests went through the ORM, that guard would raise first and the
test would pass even with the grant missing -- proving the Python, not the
permission. Raw SQL skips the guard and asks the database directly.
"""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from rcm.platform.audit import APPEND_ONLY_TABLES

pytestmark = [pytest.mark.security, pytest.mark.integration]

APP_ROLE = "rcm_app"

#: One valid row per append-only table, so each mutation targets something real.
#: A refusal on an empty table could just mean "no row matched".
INSERTS: dict[str, str] = {
    "audit_event": (
        "INSERT INTO audit_event (actor_type, actor_id, action, entity_type, entity_id) "
        "VALUES ('SYSTEM', 'test:append-only', 'probe', 'claim', :entity_id)"
    ),
    "decision_record": (
        "INSERT INTO decision_record (decision_id, entity_type, entity_id, decision_type, "
        "decided_by, logic_version, input_snapshot, output) VALUES "
        "(:row_id, 'claim', :entity_id, 'SCRUB', 'rules_engine', 'test-v0', '{}', '{}')"
    ),
}

MUTATIONS: dict[str, str] = {
    "UPDATE": "UPDATE {table} SET entity_type = 'tampered' WHERE entity_id = :entity_id",
    "DELETE": "DELETE FROM {table} WHERE entity_id = :entity_id",
    "TRUNCATE": "TRUNCATE {table}",
}


def _insert(conn: object, table: str, entity_id: uuid.UUID) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text(INSERTS[table]), {"entity_id": entity_id, "row_id": uuid.uuid4()}
    )


# ---------------------------------------------------------------------------
# The named invariant test (CLAUDE.md section 7.2)
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@pytest.mark.parametrize("operation", list(MUTATIONS))
@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
def test_audit_tables_reject_update_and_delete(
    app_engine: Engine, table: str, operation: str
) -> None:
    """As `rcm_app`: append succeeds, then every kind of modification is refused.

    The insert comes first on purpose. If the role could not insert either, a
    refused UPDATE would only show the role has no access at all -- which is a
    broken audit log, not an append-only one.
    """
    entity_id = uuid.uuid4()
    with app_engine.connect() as conn:
        transaction = conn.begin()
        try:
            _insert(conn, table, entity_id)

            with pytest.raises(ProgrammingError) as refused:
                conn.execute(
                    text(MUTATIONS[operation].format(table=table)), {"entity_id": entity_id}
                )

            assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege), (
                f"{operation} on {table} failed, but not with a privilege error: "
                f"{type(refused.value.orig).__name__}"
            )
        finally:
            # The refused statement aborted the transaction; nothing persists.
            transaction.rollback()


# ---------------------------------------------------------------------------
# The shape of the grant, read from the catalog
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
def test_catalog_shows_append_only_grants(app_engine: Engine, table: str) -> None:
    """Asks Postgres what the role holds, rather than inferring it from failures.

    Complements the test above: that one proves the refusals happen; this one
    proves no other privilege -- REFERENCES, TRIGGER -- was left behind that could
    be used to tamper indirectly. A trigger on an audit table could rewrite rows
    on insert.
    """
    with app_engine.connect() as conn:
        held = {
            privilege: conn.execute(
                text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": APP_ROLE, "table": table, "privilege": privilege},
            ).scalar_one()
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            )
        }

    assert held == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
        "REFERENCES": False,
        "TRIGGER": False,
    }, f"{APP_ROLE} privileges on {table} are not append-only: {held}"


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
def test_app_role_does_not_own_audit_tables(app_engine: Engine, table: str) -> None:
    """Ownership implies every privilege, and a REVOKE cannot take it away.

    If the application ran as the table owner, the grants in migration 0002 would
    be decorative.
    """
    with app_engine.connect() as conn:
        owner = conn.execute(
            text("SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = :t"),
            {"t": table},
        ).scalar_one()
    assert owner != APP_ROLE
