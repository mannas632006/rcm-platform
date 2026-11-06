"""Widen audit_event.action to 255 (PD-2).

`audited_session` records "<METHOD> <path>". At 64 characters a route with a
UUID and a suffix -- POST /api/v1/denials/<id>/disposition is 70 -- could not
be audited, so it was refused (fail-closed, correctly, but no such route could
ever run). Widening is expand-only; the append-only grants are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("audit_event", "action", type_=sa.String(255), existing_type=sa.String(64))


def downgrade() -> None:
    # Narrowing would truncate recorded actions; refuse rather than lose them.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM audit_event WHERE length(action) > 64) THEN
                RAISE EXCEPTION 'audit actions longer than 64 are recorded';
            END IF;
        END $$;
        """
    )
    op.alter_column("audit_event", "action", type_=sa.String(64), existing_type=sa.String(255))
