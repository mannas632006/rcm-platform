"""Two CHECK names Postgres could not hold, renamed to ones it can.

`ck_remittance_adjustment_claim_level_adjustments_have_no_service_line`
(0003) and `ck_payer_match_candidate_a_reviewed_candidate_names_its_reviewer`
(0006) are longer than Postgres's 63-character identifiers. SQLAlchemy cut
each and appended a hash when it created them, so the database never held
the name the models declare, and `alembic check` reported drift on every
run. The constraints are unchanged; only their names move.
`tests/integration/test_schema_drift.py` now fails on any drift.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, name as created, name the model declares)
RENAMES: tuple[tuple[str, str, str], ...] = (
    (
        "remittance_adjustment",
        "ck_remittance_adjustment_claim_level_adjustments_have_n_2f9f",
        "ck_remittance_adjustment_claim_level_has_no_line",
    ),
    (
        "payer_match_candidate",
        "ck_payer_match_candidate_a_reviewed_candidate_names_its_c13f",
        "ck_payer_match_candidate_reviewed_names_reviewer",
    ),
)


def upgrade() -> None:
    for table, old, new in RENAMES:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}"')


def downgrade() -> None:
    for table, old, new in RENAMES:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{new}" TO "{old}"')
