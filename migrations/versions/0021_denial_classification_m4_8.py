"""Denial classifications (M4.8, ADR-0012).

One row per denied 835 line per taxonomy version. Insert-only by grant:
removing the REVOKE fails `test_a_classification_cannot_be_rewritten`.

Expand-only. The unique constraint added to `remittance_service_line` is
implied by its primary key, so no existing row can violate it; it exists to
be the target of the composite foreign key that pins each classification to
its line's client and posting (PD-20).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
CATEGORIES = (
    "'AUTHORISATION', 'CODING', 'CONTRACTUAL_NON_COVERED', 'COORDINATION_OF_BENEFITS', "
    "'DUPLICATE', 'MEDICAL_NECESSITY', 'PAYER_ERROR', 'PROVIDER_ENROLMENT', "
    "'REGISTRATION_ELIGIBILITY', 'TIMELY_FILING'"
)
PREVENTABILITY = "'NOT_PREVENTABLE', 'PARTIALLY_PREVENTABLE', 'PREVENTABLE'"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_remittance_service_line_line_client_payment",
        "remittance_service_line",
        ["service_line_id", "client_id", "claim_payment_id"],
    )
    op.create_table(
        "denial_classification",
        sa.Column("service_line_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("taxonomy_version", sa.String(32), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("preventability", sa.String(24), nullable=False),
        sa.Column("cas_group_code", sa.String(2), nullable=False),
        sa.Column("carc_code", sa.String(8), nullable=False),
        sa.Column(
            "classified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["service_line_id", "client_id", "claim_payment_id"],
            [
                "remittance_service_line.service_line_id",
                "remittance_service_line.client_id",
                "remittance_service_line.claim_payment_id",
            ],
            name=op.f("fk_denial_classification_line"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_payment_id"],
            ["claim_payment.claim_payment_id"],
            name=op.f("fk_denial_classification_claim_payment_id_claim_payment"),
        ),
        sa.CheckConstraint(
            f"category IN ({CATEGORIES})",
            name=op.f("ck_denial_classification_category_is_known"),
        ),
        sa.CheckConstraint(
            f"preventability IN ({PREVENTABILITY})",
            name=op.f("ck_denial_classification_preventability_is_known"),
        ),
        sa.CheckConstraint(
            "cas_group_code IN ('CO', 'PR', 'OA', 'PI', 'CR')",
            name=op.f("ck_denial_classification_cas_group_code_is_valid"),
        ),
        sa.PrimaryKeyConstraint(
            "service_line_id", "taxonomy_version", name=op.f("pk_denial_classification")
        ),
    )
    op.create_index(
        "ix_denial_classification_client_version",
        "denial_classification",
        ["client_id", "taxonomy_version"],
    )
    op.create_index(
        "ix_denial_classification_client_category",
        "denial_classification",
        ["client_id", "category"],
    )
    op.create_index(
        "ix_denial_classification_claim_payment", "denial_classification", ["claim_payment_id"]
    )
    op.execute(f"REVOKE ALL ON TABLE denial_classification FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE denial_classification TO {APP_ROLE}")


def downgrade() -> None:
    # No rollback once anything is classified: decision records cite the rows.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM denial_classification) THEN
                RAISE EXCEPTION 'denials are classified; decision records cite them';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_denial_classification_claim_payment", table_name="denial_classification")
    op.drop_index("ix_denial_classification_client_category", table_name="denial_classification")
    op.drop_index("ix_denial_classification_client_version", table_name="denial_classification")
    op.drop_table("denial_classification")
    op.drop_constraint(
        "uq_remittance_service_line_line_client_payment",
        "remittance_service_line",
        type_="unique",
    )
