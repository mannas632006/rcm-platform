"""Rule versions and published rulesets (M4.1, ADR-0007).

- `rule_version`: one row per rule version. The application role may insert
  and change `status` only; content is write-once (ADR-0007 Decision 1).
- `ruleset` / `ruleset_member`: published snapshots, insert-only.

Write-once is enforced by grants, the mechanism migration 0002 uses for the
audit tables: removing a REVOKE here fails
`test_a_stored_rule_cannot_be_rewritten` and `test_a_ruleset_cannot_be_rewritten`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
LIVE = "status IN ('ADVISORY', 'BLOCKING', 'SHADOW')"


def upgrade() -> None:
    op.create_table(
        "rule_version",
        sa.Column("rule_id", sa.String(120), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("layer", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("version >= 1", name="ck_rule_version_positive"),
        sa.CheckConstraint(
            "layer IN ('CLINICAL', 'DEMOGRAPHIC', 'LEARNED', 'PAYER_SPECIFIC', "
            "'STRUCTURAL', 'TEMPORAL')",
            name="ck_rule_version_layer",
        ),
        sa.CheckConstraint(
            "severity IN ('ADVISORY', 'BLOCKING', 'INFORMATIONAL')",
            name="ck_rule_version_severity",
        ),
        sa.CheckConstraint(
            "status IN ('ADVISORY', 'BACKTESTING', 'BLOCKING', 'DEPRECATED', 'DRAFT', 'SHADOW')",
            name="ck_rule_version_status",
        ),
    )
    op.create_index(
        "uq_rule_version_one_live",
        "rule_version",
        ["rule_id"],
        unique=True,
        postgresql_where=sa.text(LIVE),
    )

    op.create_table(
        "ruleset",
        sa.Column("ruleset_version", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("rule_count", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.String(128), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("rule_count >= 0", name="ck_ruleset_rule_count"),
    )

    op.create_table(
        "ruleset_member",
        sa.Column(
            "ruleset_version",
            sa.Integer(),
            sa.ForeignKey("ruleset.ruleset_version"),
            primary_key=True,
        ),
        sa.Column("rule_id", sa.String(120), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id", "version"],
            ["rule_version.rule_id", "rule_version.version"],
            name="fk_ruleset_member_rule_version",
        ),
        sa.CheckConstraint(LIVE, name="ck_ruleset_member_status"),
    )

    # Default privileges give the application role full DML on new tables.
    # Take it back, and grant only what write-once needs.
    for table in ("rule_version", "ruleset", "ruleset_member"):
        op.execute(f"REVOKE ALL ON TABLE {table} FROM {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {APP_ROLE}")
    op.execute(f"GRANT UPDATE (status) ON TABLE rule_version TO {APP_ROLE}")


def downgrade() -> None:
    # Refuse to drop published rulesets: claims' decision records name them.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM ruleset) THEN
                RAISE EXCEPTION 'rulesets have been published; decision records cite them';
            END IF;
        END $$;
        """
    )
    op.drop_table("ruleset_member")
    op.drop_table("ruleset")
    op.drop_index("uq_rule_version_one_live", table_name="rule_version")
    op.drop_table("rule_version")
