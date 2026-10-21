"""Make the audit tables append-only for the application role (DR-06, S-10)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02

This migration *is* the control. `rcm.platform.audit` carries an ORM-level guard,
but the application can edit its own source; it cannot edit its own grants. After
this runs, `UPDATE audit_event` from the application role fails in the database,
regardless of what the code says.

`tests/security/test_append_only_audit.py` connects as `rcm_app` and asserts the
refusal, so removing these REVOKEs fails the build.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("audit_event", "decision_record")
APP_ROLE = "rcm_app"


def upgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        # Grant exactly what an append-only writer needs, and nothing else.
        # SELECT so operators can read the trail; INSERT so the platform can add
        # to it. No UPDATE. No DELETE. No TRUNCATE (which DELETE grants do not
        # cover anyway -- TRUNCATE requires ownership, which rcm_app lacks).
        op.execute(f"REVOKE ALL ON TABLE {table} FROM {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {APP_ROLE}")

    # audit_event.event_id is a BIGSERIAL; inserting requires the sequence.
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE audit_event_event_id_seq TO {APP_ROLE}")

    # Deliberately NOT altering schema-wide default privileges here. Append-only
    # is a property of specific tables, granted per table above. Revoking
    # UPDATE/DELETE by default would strip them from every table created later
    # -- including `claim`, whose state transitions are updates -- and surface
    # months from now as a permission error nowhere near this file. A future
    # append-only table gets its own explicit REVOKE in its own migration, and
    # joins APPEND_ONLY_TABLES in rcm.platform.audit so the test covers it.


def downgrade() -> None:
    # Restoring UPDATE and DELETE re-opens the audit trail to modification.
    # Present for completeness; running it in a deployed environment is a
    # compliance decision, not an engineering one.
    for table in APPEND_ONLY_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE}")
