"""Write-off requests and their approvals (M5.7, BR-M11-05).

Both tables are insert-only by grant: a request, and the one decision on it,
are records of who asked for how much and who agreed. The application role
cannot rewrite them. Expand-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"

REASONS = ("APPEAL_NOT_VIABLE", "TIMELY_FILING_MISSED", "CONTRACTUAL", "SMALL_BALANCE", "OTHER")
DECISIONS = ("APPROVED", "REJECTED")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "write_off",
        sa.Column("write_off_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("needs_approval", sa.Boolean(), nullable=False),
        sa.Column("threshold", sa.Numeric(12, 2), nullable=False),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_item.work_item_id"],
            name=op.f("fk_write_off_work_item_id_work_item"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.claim_id"], name=op.f("fk_write_off_claim_id_claim")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_write_off_write_off_amount_is_positive")),
        sa.CheckConstraint(
            _in("reason", REASONS), name=op.f("ck_write_off_write_off_reason_is_known")
        ),
        sa.CheckConstraint(
            "reason <> 'OTHER' OR length(trim(note)) > 0",
            name=op.f("ck_write_off_write_off_other_says_what"),
        ),
        sa.CheckConstraint(
            "length(trim(requested_by)) > 0",
            name=op.f("ck_write_off_write_off_names_its_requester"),
        ),
        sa.PrimaryKeyConstraint("write_off_id", name=op.f("pk_write_off")),
    )
    op.create_index("ix_write_off_item", "write_off", ["client_id", "work_item_id"])

    op.create_table(
        "write_off_decision",
        sa.Column("write_off_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["write_off_id"],
            ["write_off.write_off_id"],
            name=op.f("fk_write_off_decision_write_off_id_write_off"),
        ),
        sa.CheckConstraint(
            _in("decision", DECISIONS),
            name=op.f("ck_write_off_decision_write_off_decision_is_known"),
        ),
        sa.CheckConstraint(
            "length(trim(decided_by)) > 0",
            name=op.f("ck_write_off_decision_write_off_decision_names_its_maker"),
        ),
        sa.PrimaryKeyConstraint("write_off_id", name=op.f("pk_write_off_decision")),
    )

    for table in ("write_off", "write_off_decision"):
        op.execute(f"REVOKE ALL ON TABLE {table} FROM {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {APP_ROLE}")


def downgrade() -> None:
    # A recorded write-off moved money on a claim; dropping it would lose who
    # agreed to that.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM write_off) THEN
                RAISE EXCEPTION 'write-offs are recorded; they cannot be dropped';
            END IF;
        END $$;
        """
    )
    op.drop_table("write_off_decision")
    op.drop_index("ix_write_off_item", table_name="write_off")
    op.drop_table("write_off")
