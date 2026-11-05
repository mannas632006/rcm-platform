"""Rule status changes (M4.5, M4.6, ADR-0010).

Insert-only by grant: the history of who promoted which rule, on what
evidence, is never edited. Removing the REVOKE fails
`test_a_status_change_record_cannot_be_rewritten`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"


def upgrade() -> None:
    op.create_table(
        "rule_status_change",
        sa.Column("change_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("rule_id", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=False),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("changed_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "backtest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rule_backtest.backtest_id"),
            nullable=True,
        ),
        sa.Column("fp_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["rule_id", "version"],
            ["rule_version.rule_id", "rule_version.version"],
            name="fk_rule_status_change_rule_version",
        ),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_rule_status_change_reason"),
        sa.CheckConstraint(
            "to_status NOT IN ('SHADOW', 'BLOCKING') OR backtest_id IS NOT NULL",
            name="ck_rule_status_change_evidence",
        ),
    )
    op.create_index(
        "ix_rule_status_change_rule", "rule_status_change", ["rule_id", "version", "sequence"]
    )
    op.execute(f"REVOKE ALL ON TABLE rule_status_change FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE rule_status_change TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM rule_status_change) THEN
                RAISE EXCEPTION 'rule promotions are recorded; their history is kept';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_rule_status_change_rule", table_name="rule_status_change")
    op.drop_table("rule_status_change")
