"""Payer registry entries (M4.10, DOC-02 9, ADR-0014).

Versioned and insert-only by grant (BR-REG-03): a change or a
re-verification is a new row, and the application role cannot rewrite
history -- removing the REVOKE fails
`test_a_change_is_a_new_version_and_the_old_one_stays`. Expand-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"


def upgrade() -> None:
    op.create_table(
        "payer_registry_entry",
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payer_id", sa.String(80), nullable=False),
        sa.Column("plan_type", sa.String(64), nullable=False),
        sa.Column("cpt", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prior_auth_required", sa.Boolean(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("last_verified_date", sa.Date(), nullable=False),
        sa.Column("verified_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_by", sa.String(128), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["payer_id"],
            ["payer.payer_id"],
            name=op.f("fk_payer_registry_entry_payer_id_payer"),
        ),
        sa.UniqueConstraint(
            "payer_id", "plan_type", "cpt", "version", name="uq_payer_registry_entry_version"
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_payer_registry_entry_version_positive")),
        sa.CheckConstraint(
            "length(trim(verified_by)) > 0",
            name=op.f("ck_payer_registry_entry_names_its_verifier"),
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_payer_registry_entry_states_a_reason")
        ),
        sa.CheckConstraint(
            "length(plan_type) > 0 AND length(cpt) > 0",
            name=op.f("ck_payer_registry_entry_keys_are_named"),
        ),
        sa.PrimaryKeyConstraint("entry_id", name=op.f("pk_payer_registry_entry")),
    )
    op.create_index(
        "ix_payer_registry_entry_payer",
        "payer_registry_entry",
        ["payer_id", "cpt", "plan_type", "version"],
    )
    op.execute(f"REVOKE ALL ON TABLE payer_registry_entry FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE payer_registry_entry TO {APP_ROLE}")


def downgrade() -> None:
    # Scrub decision records cite entry versions; with entries recorded there
    # is no rollback that keeps them answerable.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM payer_registry_entry) THEN
                RAISE EXCEPTION 'registry entries are recorded; scrub records cite them';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_payer_registry_entry_payer", table_name="payer_registry_entry")
    op.drop_table("payer_registry_entry")
