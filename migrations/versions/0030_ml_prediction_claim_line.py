"""A prediction can name the submitted line it scored (ADR-0032).

ML-1 decides before a claim is sent, when the 835's service line does not
exist yet. `claim_line_id` names the submitted line instead:

- a SCORED row must name a line of one kind or the other;
- a line scored at submission (no service line yet) is scored once per model
  version, as an adjudicated line already was.

Expand-only: a nullable column, a wider check, two indexes. Rows written
before this migration satisfy the wider check as they are.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)

_EXPLAINED = "(status = 'DEGRADED' AND score IS NULL AND reason IS NOT NULL)"
_SCORED = (
    "(status = 'SCORED' AND score IS NOT NULL AND score >= 0 AND score <= 1 "
    "AND decision_id IS NOT NULL AND {line})"
)
NEW = (
    _SCORED.format(line="(service_line_id IS NOT NULL OR claim_line_id IS NOT NULL)")
    + " OR "
    + _EXPLAINED
)
OLD = _SCORED.format(line="service_line_id IS NOT NULL") + " OR " + _EXPLAINED

AT_SUBMISSION = "service_line_id IS NULL AND claim_line_id IS NOT NULL"


def upgrade() -> None:
    op.add_column("ml_prediction", sa.Column("claim_line_id", UUID, nullable=True))
    # Bare names: the naming convention adds the prefix.
    op.drop_constraint("scored_or_explained", "ml_prediction", type_="check")
    op.create_check_constraint("scored_or_explained", "ml_prediction", NEW)
    op.create_index(
        "uq_ml_prediction_version_claim_line_at_submission",
        "ml_prediction",
        ["model_version_id", "claim_line_id"],
        unique=True,
        postgresql_where=sa.text(AT_SUBMISSION),
    )
    op.create_index("ix_ml_prediction_claim_line_id", "ml_prediction", ["claim_line_id"])


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM ml_prediction WHERE claim_line_id IS NOT NULL) THEN
                RAISE EXCEPTION
                    'predictions name a submitted line; they cannot be dropped';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_ml_prediction_claim_line_id", table_name="ml_prediction")
    op.drop_index("uq_ml_prediction_version_claim_line_at_submission", table_name="ml_prediction")
    op.drop_constraint("scored_or_explained", "ml_prediction", type_="check")
    op.create_check_constraint("scored_or_explained", "ml_prediction", OLD)
    op.drop_column("ml_prediction", "claim_line_id")
