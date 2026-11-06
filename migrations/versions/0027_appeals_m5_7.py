"""Appeals: the appeal, each version of its letter, and its sign-off (M5.7, ADR-0017).

All three are insert-only by grant. UR-09 asks that no send bypasses a
person's review; that is only provable if what was drafted, changed and
signed -- and by whom -- cannot be rewritten afterwards. Expand-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "appeal",
        sa.Column("appeal_id", UUID, nullable=False),
        sa.Column("client_id", UUID, nullable=False),
        sa.Column("work_item_id", UUID, nullable=False),
        sa.Column("claim_id", UUID, nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_item.work_item_id"],
            name=op.f("fk_appeal_work_item_id_work_item"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.claim_id"], name=op.f("fk_appeal_claim_id_claim")
        ),
        sa.CheckConstraint(
            "length(trim(created_by)) > 0", name=op.f("ck_appeal_appeal_names_its_author")
        ),
        sa.PrimaryKeyConstraint("appeal_id", name=op.f("pk_appeal")),
    )
    op.create_index("ix_appeal_item", "appeal", ["client_id", "work_item_id"])

    op.create_table(
        "appeal_version",
        sa.Column("appeal_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("client_id", UUID, nullable=False),
        sa.Column("letter", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("written_by", sa.String(128), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["appeal_id"], ["appeal.appeal_id"], name=op.f("fk_appeal_version_appeal_id_appeal")
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_appeal_version_appeal_version_is_positive")
        ),
        sa.CheckConstraint(
            "length(trim(letter)) > 0", name=op.f("ck_appeal_version_appeal_version_has_a_letter")
        ),
        sa.CheckConstraint(
            "length(trim(written_by)) > 0",
            name=op.f("ck_appeal_version_appeal_version_names_its_writer"),
        ),
        sa.PrimaryKeyConstraint("appeal_id", "version", name=op.f("pk_appeal_version")),
    )

    op.create_table(
        "appeal_signoff",
        sa.Column("appeal_id", UUID, nullable=False),
        sa.Column("client_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("letter_sha256", sa.String(64), nullable=False),
        sa.Column("signed_by", sa.String(128), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["appeal_id"], ["appeal.appeal_id"], name=op.f("fk_appeal_signoff_appeal_id_appeal")
        ),
        sa.CheckConstraint(
            "length(letter_sha256) = 64",
            name=op.f("ck_appeal_signoff_appeal_signoff_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "length(trim(signed_by)) > 0",
            name=op.f("ck_appeal_signoff_appeal_signoff_names_its_signer"),
        ),
        sa.PrimaryKeyConstraint("appeal_id", name=op.f("pk_appeal_signoff")),
    )

    for table in ("appeal", "appeal_version", "appeal_signoff"):
        op.execute(f"REVOKE ALL ON TABLE {table} FROM {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {APP_ROLE}")


def downgrade() -> None:
    # A recorded appeal may have gone to a payer; dropping it would lose who
    # read and signed what was sent.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM appeal) THEN
                RAISE EXCEPTION 'appeals are recorded; they cannot be dropped';
            END IF;
        END $$;
        """
    )
    op.drop_table("appeal_signoff")
    op.drop_table("appeal_version")
    op.drop_index("ix_appeal_item", table_name="appeal")
    op.drop_table("appeal")
