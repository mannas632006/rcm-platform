"""Rule backtests (M4.4, ADR-0009).

Insert-only by grant: M4.5 promotes rules on what is recorded here, so a
recorded backtest is never edited. Removing the REVOKE fails
`test_a_backtest_record_cannot_be_rewritten`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"


def upgrade() -> None:
    op.create_table(
        "rule_backtest",
        sa.Column("backtest_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_id", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claims_evaluated", sa.Integer(), nullable=False),
        sa.Column("flagged", sa.Integer(), nullable=False),
        sa.Column("true_positives", sa.Integer(), nullable=False),
        sa.Column("false_positives", sa.Integer(), nullable=False),
        sa.Column("missed_denials", sa.Integer(), nullable=False),
        sa.Column("indeterminate", sa.Integer(), nullable=False),
        sa.Column("without_outcome", sa.Integer(), nullable=False),
        sa.Column("fp_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("ran_by", sa.String(128), nullable=False),
        sa.Column(
            "ran_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["rule_id", "version"],
            ["rule_version.rule_id", "rule_version.version"],
            name="fk_rule_backtest_rule_version",
        ),
        sa.CheckConstraint(
            "flagged = true_positives + false_positives AND flagged <= claims_evaluated",
            name="ck_rule_backtest_counts",
        ),
        sa.CheckConstraint(
            "(flagged = 0 AND fp_rate IS NULL) OR (flagged > 0 AND fp_rate BETWEEN 0 AND 1)",
            name="ck_rule_backtest_fp_rate",
        ),
    )
    op.create_index("ix_rule_backtest_rule", "rule_backtest", ["rule_id", "version", "ran_at"])
    op.execute(f"REVOKE ALL ON TABLE rule_backtest FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE rule_backtest TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM rule_backtest) THEN
                RAISE EXCEPTION 'backtests are recorded; rule promotions rest on them';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_rule_backtest_rule", table_name="rule_backtest")
    op.drop_table("rule_backtest")
