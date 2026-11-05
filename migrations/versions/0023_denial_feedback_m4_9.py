"""Denial feedback (M4.9, ADR-0013).

One row per classified denial per taxonomy version: the candidate rule it
drafted or joined, the rule that failed it, or the recorded reason no rule
was drafted (FR-RULES-07). Insert-only by grant.

Expand-only. The unique constraint added to `denial_classification` is
implied by its primary key, so no row can violate it; it is the target of
the composite foreign key that ties feedback to its classification's client
and posting (PD-20).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
OUTCOMES = (
    "'ALREADY_COVERED', 'CANDIDATE_DRAFTED', 'CANDIDATE_EXISTS', 'LINE_UNMATCHED', "
    "'NOT_PREVENTABLE', 'NO_CLAIM', 'NO_RULE', 'RULE_FAILED'"
)
_TARGET = "uq_denial_classification_line_version_client_payment"


def upgrade() -> None:
    op.create_unique_constraint(
        _TARGET,
        "denial_classification",
        ["service_line_id", "taxonomy_version", "client_id", "claim_payment_id"],
    )
    op.create_table(
        "denial_feedback",
        sa.Column("service_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("taxonomy_version", sa.String(32), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("rule_id", sa.String(120), nullable=True),
        sa.Column("rule_version", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["service_line_id", "taxonomy_version", "client_id", "claim_payment_id"],
            [
                "denial_classification.service_line_id",
                "denial_classification.taxonomy_version",
                "denial_classification.client_id",
                "denial_classification.claim_payment_id",
            ],
            name=op.f("fk_denial_feedback_classification"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.claim_id"], name=op.f("fk_denial_feedback_claim_id_claim")
        ),
        sa.CheckConstraint(
            f"outcome IN ({OUTCOMES})", name=op.f("ck_denial_feedback_outcome_is_known")
        ),
        sa.CheckConstraint(
            "(outcome IN ('CANDIDATE_DRAFTED', 'CANDIDATE_EXISTS', 'RULE_FAILED', "
            "'ALREADY_COVERED')) = (rule_id IS NOT NULL)",
            name=op.f("ck_denial_feedback_names_its_rule"),
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_denial_feedback_states_a_reason")
        ),
        sa.PrimaryKeyConstraint(
            "service_line_id", "taxonomy_version", name=op.f("pk_denial_feedback")
        ),
    )
    op.create_index(
        "ix_denial_feedback_client_outcome", "denial_feedback", ["client_id", "outcome"]
    )
    op.create_index("ix_denial_feedback_rule", "denial_feedback", ["rule_id"])
    op.execute(f"REVOKE ALL ON TABLE denial_feedback FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE denial_feedback TO {APP_ROLE}")


def downgrade() -> None:
    # No rollback once feedback exists: decision records cite the rows.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM denial_feedback) THEN
                RAISE EXCEPTION 'denial feedback is recorded; decision records cite it';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_denial_feedback_rule", table_name="denial_feedback")
    op.drop_index("ix_denial_feedback_client_outcome", table_name="denial_feedback")
    op.drop_table("denial_feedback")
    op.drop_constraint(_TARGET, "denial_classification", type_="unique")
