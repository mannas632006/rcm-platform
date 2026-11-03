"""NCCI releases, pinned by rulesets (M4.3, ADR-0008).

- `ncci_release`, `ncci_ptp_edit`, `ncci_mue`: insert-only by grant, like
  rulesets (migration 0017). Removing a REVOKE fails
  `test_ncci_rows_cannot_be_rewritten`.
- `ruleset` gains the PTP and MUE releases it pins. Nullable: rulesets
  published before this migration pinned none, and say so.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
TABLES = ("ncci_release", "ncci_ptp_edit", "ncci_mue")


def upgrade() -> None:
    op.create_table(
        "ncci_release",
        sa.Column("release_id", sa.String(40), primary_key=True),
        sa.Column("kind", sa.String(3), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("loaded_by", sa.String(128), nullable=False),
        sa.Column(
            "loaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("kind IN ('MUE', 'PTP')", name="ck_ncci_release_kind"),
        sa.CheckConstraint("release_id = kind || ':' || label", name="ck_ncci_release_id"),
        sa.CheckConstraint("row_count > 0", name="ck_ncci_release_rows"),
    )
    op.create_table(
        "ncci_ptp_edit",
        sa.Column(
            "release_id",
            sa.String(40),
            sa.ForeignKey("ncci_release.release_id"),
            primary_key=True,
        ),
        sa.Column("column_1", sa.String(5), primary_key=True),
        sa.Column("column_2", sa.String(5), primary_key=True),
        sa.Column("effective_date", sa.Date(), primary_key=True),
        sa.Column("deletion_date", sa.Date(), nullable=True),
        sa.Column("modifier_indicator", sa.String(1), nullable=False),
        sa.CheckConstraint("modifier_indicator IN ('0', '1', '9')", name="ck_ncci_ptp_indicator"),
        sa.CheckConstraint(
            "deletion_date IS NULL OR deletion_date > effective_date", name="ck_ncci_ptp_dates"
        ),
    )
    op.create_table(
        "ncci_mue",
        sa.Column(
            "release_id",
            sa.String(40),
            sa.ForeignKey("ncci_release.release_id"),
            primary_key=True,
        ),
        sa.Column("code", sa.String(5), primary_key=True),
        sa.Column("mue_value", sa.Numeric(9, 2), nullable=False),
        sa.Column("adjudication_indicator", sa.String(1), nullable=False),
        sa.CheckConstraint("mue_value > 0", name="ck_ncci_mue_value"),
        sa.CheckConstraint(
            "adjudication_indicator IN ('1', '2', '3')", name="ck_ncci_mue_indicator"
        ),
    )
    for table in TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {APP_ROLE}")

    for column in ("ncci_ptp_release", "ncci_mue_release"):
        op.add_column(
            "ruleset",
            sa.Column(
                column, sa.String(40), sa.ForeignKey("ncci_release.release_id"), nullable=True
            ),
        )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM ruleset
                WHERE ncci_ptp_release IS NOT NULL OR ncci_mue_release IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'a published ruleset pins an NCCI release';
            END IF;
        END $$;
        """
    )
    op.drop_column("ruleset", "ncci_mue_release")
    op.drop_column("ruleset", "ncci_ptp_release")
    for table in reversed(TABLES):
        op.drop_table(table)
