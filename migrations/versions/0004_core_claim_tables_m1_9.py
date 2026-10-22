"""CORE claim tables with the resubmission chain (M1.9).

Adds `claim` and `claim_line`, the submission side of CORE, following the
schema in SRS section 5.2.

The constraints here are the milestone. BR-DATA-03 requires resubmissions to
chain to the claim they replace rather than being stored as independent
claims, "because failure to do this inflates volume and distorts the denial
rate" -- on the synthetic corpus, 616 submissions are 600 claims, and counting
submissions reports a denial rate of 17.37% where the truth is 15.17%.

So the chain is recorded as an edge (`original_claim_id`, from REF*F8) and
again already resolved (`root_claim_id`), and three CHECK constraints keep the
two from drifting:

    claim_does_not_replace_itself   no one-row cycle
    root_agrees_with_the_chain      an original is its own root, a
                                    resubmission's root is an earlier claim
    attempt_agrees_with_the_chain   and the attempt number agrees with both

`line_has_a_diagnosis_pointer` enforces BR-M6-01 at the table, not only in the
loader that happens to write it today.

Rollback: `downgrade` drops both tables. Nothing else references them yet, so
it is clean -- the remittance side keys claims by the payer's own claim number
as text and does not join to `claim`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "claim",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("raw_file_id", sa.UUID(), nullable=False),
        sa.Column("submitter_claim_id", sa.String(length=50), nullable=False),
        sa.Column("original_claim_id", sa.UUID(), nullable=True),
        sa.Column("root_claim_id", sa.UUID(), nullable=False),
        sa.Column("submission_attempt", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("claim_frequency_code", sa.String(length=2), server_default="1", nullable=False),
        sa.Column("payer_id", sa.String(length=80), nullable=False),
        sa.Column("payer_responsibility", sa.String(length=2), server_default="P", nullable=False),
        sa.Column("claim_filing_code", sa.String(length=4), server_default="", nullable=False),
        sa.Column("billing_npi", sa.String(length=20), nullable=False),
        sa.Column("billing_tax_id", sa.String(length=20), server_default="", nullable=False),
        sa.Column("subscriber_member_id", sa.String(length=80), nullable=False),
        sa.Column("practice_identifier", sa.String(length=80), server_default="", nullable=False),
        sa.Column("place_of_service", sa.String(length=4), server_default="", nullable=False),
        sa.Column("date_of_service", sa.Date(), nullable=True),
        sa.Column("total_charge", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("prior_auth_number", sa.String(length=50), server_default="", nullable=False),
        sa.Column("referral_number", sa.String(length=50), server_default="", nullable=False),
        sa.Column("diagnoses", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(original_claim_id IS NULL AND root_claim_id = claim_id) OR (original_claim_id IS NOT NULL AND root_claim_id <> claim_id)",
            name=op.f("ck_claim_root_agrees_with_the_chain"),
        ),
        sa.CheckConstraint(
            "(original_claim_id IS NULL AND submission_attempt = 1) OR (original_claim_id IS NOT NULL AND submission_attempt > 1)",
            name=op.f("ck_claim_attempt_agrees_with_the_chain"),
        ),
        sa.CheckConstraint(
            "length(submitter_claim_id) > 0", name=op.f("ck_claim_submitter_claim_id_is_present")
        ),
        sa.CheckConstraint(
            "original_claim_id <> claim_id", name=op.f("ck_claim_claim_does_not_replace_itself")
        ),
        sa.CheckConstraint("total_charge >= 0", name=op.f("ck_claim_total_charge_is_not_negative")),
        sa.ForeignKeyConstraint(
            ["original_claim_id"],
            ["claim.claim_id"],
            name=op.f("fk_claim_original_claim_id_claim"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payer_id"], ["payer.payer_id"], name=op.f("fk_claim_payer_id_payer")
        ),
        sa.ForeignKeyConstraint(
            ["raw_file_id"], ["raw_file.raw_file_id"], name=op.f("fk_claim_raw_file_id_raw_file")
        ),
        sa.ForeignKeyConstraint(
            ["root_claim_id"],
            ["claim.claim_id"],
            name=op.f("fk_claim_root_claim_id_claim"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("claim_id", name=op.f("pk_claim")),
        sa.UniqueConstraint("client_id", "submitter_claim_id", name="uq_claim_client_submitter_id"),
    )
    op.create_index("ix_claim_payer", "claim", ["client_id", "payer_id"], unique=False)
    op.create_index("ix_claim_root", "claim", ["client_id", "root_claim_id"], unique=False)
    op.create_table(
        "claim_line",
        sa.Column("claim_line_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("line_number", sa.SmallInteger(), nullable=False),
        sa.Column("procedure_code", sa.String(length=16), nullable=False),
        sa.Column("modifier_1", sa.String(length=2), nullable=True),
        sa.Column("modifier_2", sa.String(length=2), nullable=True),
        sa.Column("modifier_3", sa.String(length=2), nullable=True),
        sa.Column("modifier_4", sa.String(length=2), nullable=True),
        sa.Column("units", sa.Numeric(precision=12, scale=3), server_default="1", nullable=False),
        sa.Column("charge_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "diagnosis_pointers",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("service_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cardinality(diagnosis_pointers) >= 1",
            name=op.f("ck_claim_line_line_has_a_diagnosis_pointer"),
        ),
        sa.CheckConstraint(
            "charge_amount >= 0", name=op.f("ck_claim_line_charge_amount_is_not_negative")
        ),
        sa.CheckConstraint(
            "length(procedure_code) > 0", name=op.f("ck_claim_line_procedure_code_is_present")
        ),
        sa.CheckConstraint("line_number > 0", name=op.f("ck_claim_line_line_number_is_positive")),
        sa.CheckConstraint("units > 0", name=op.f("ck_claim_line_units_are_positive")),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claim.claim_id"],
            name=op.f("fk_claim_line_claim_id_claim"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("claim_line_id", name=op.f("pk_claim_line")),
        sa.UniqueConstraint("claim_id", "line_number", name="uq_claim_line_number"),
    )
    op.create_index("ix_claim_line_claim", "claim_line", ["client_id", "claim_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_claim_line_claim", table_name="claim_line")
    op.drop_table("claim_line")
    op.drop_index("ix_claim_root", table_name="claim")
    op.drop_index("ix_claim_payer", table_name="claim")
    op.drop_table("claim")
