"""Rejections, kept apart from denials by the schema (M1.6).

Adds `claim_acknowledgement`: one verdict on one claim, from a 999, a 277CA
or a 277.

    BR-GLOBAL-02 (MUST): Rejections and denials MUST be stored and reported
    as distinct outcome types. Conflating them inflates the denial rate and
    hides front-end data quality problems.

The separation is structural rather than a flag. Rejections land in this
table; denials are derived from `remittance_service_line` and
`remittance_adjustment`. A rejection therefore cannot reach the denial rate
however that query is written, because the rows are not in the tables it
reads -- the same argument the remittance loader makes for never creating a
claim row.

`outcome` has two values and `DENIED` is not one of them. A denial is what an
835 says about a claim that *was* adjudicated, and the absence of the word
from this enum is the point.

`source` is recorded because BR-M8-02 makes the distinction load-bearing: a
999 acceptance confirms syntax and is explicitly not submission, while a
277CA acceptance is. A query that cannot tell them apart cannot answer
whether a claim is submitted.

Rollback: `downgrade` drops the table. Nothing references it -- deliberately,
since an acknowledgement can arrive for a claim this platform never loaded and
losing the record of a rejection because its claim is missing is the wrong
trade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "claim_acknowledgement",
        sa.Column("acknowledgement_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("raw_file_id", sa.UUID(), nullable=False),
        sa.Column("claim_id", sa.String(length=80), nullable=False),
        sa.Column("payer_claim_number", sa.String(length=80), server_default="", nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("category_code", sa.String(length=8), server_default="", nullable=False),
        sa.Column("status_code", sa.String(length=8), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome <> 'REJECTED' OR source = '999' OR length(category_code) > 0",
            name=op.f("ck_claim_acknowledgement_a_rejection_names_its_reason"),
        ),
        sa.CheckConstraint(
            "outcome IN ('ACCEPTED', 'REJECTED')",
            name=op.f("ck_claim_acknowledgement_outcome_is_known"),
        ),
        sa.CheckConstraint(
            "source IN ('999', '277CA', '277')",
            name=op.f("ck_claim_acknowledgement_source_is_known"),
        ),
        sa.CheckConstraint(
            "length(claim_id) > 0",
            name=op.f("ck_claim_acknowledgement_acknowledgement_names_its_claim"),
        ),
        sa.ForeignKeyConstraint(
            ["raw_file_id"],
            ["raw_file.raw_file_id"],
            name=op.f("fk_claim_acknowledgement_raw_file_id_raw_file"),
        ),
        sa.PrimaryKeyConstraint("acknowledgement_id", name=op.f("pk_claim_acknowledgement")),
        sa.UniqueConstraint(
            "raw_file_id", "claim_id", "source", "category_code", name="uq_acknowledgement_fact"
        ),
    )
    op.create_index(
        "ix_acknowledgement_client_claim",
        "claim_acknowledgement",
        ["client_id", "claim_id"],
        unique=False,
    )
    op.create_index(
        "ix_acknowledgement_outcome",
        "claim_acknowledgement",
        ["client_id", "outcome"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_acknowledgement_outcome", table_name="claim_acknowledgement")
    op.drop_index("ix_acknowledgement_client_claim", table_name="claim_acknowledgement")
    op.drop_table("claim_acknowledgement")
