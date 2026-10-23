"""Link reversals and recoupments to what they act on (M1.10).

Two ways a payer takes money back, and until now neither was connected to the
payment it undoes.

    reversal     CLP02 = 22. The payer re-adjudicates a claim it already paid
                 and sends a later remittance with the amounts negated.
                 BR-M9-04: this "MUST reverse the original posting and re-open
                 the claim, not create a new negative claim".

    recoupment   A PLB entry with reason WO. The payer reduces *today's*
                 cheque to recover an earlier overpayment (EC-47), naming the
                 claim in PLB03-2.

Both are matched on the payer's own control number, which DOC-02 section 6
singles out for exactly this: *"The payer's claim number (CLP07) is how you
match resubmissions and reversals. Store it."*

`is_reversal` is denormalised from `status_code` on purpose. Every query that
must exclude reversals -- the denial rate first among them -- would otherwise
carry a `status_code <> '22'` that is easy to leave out and silent when it is.

The two CHECK constraints are written by hand because Alembic's autogenerate
does not compare CHECK constraints: declaring them on the model alone would
leave them in the ORM and absent from the database, which is the shape of
defect M1.9 found in `line_has_a_diagnosis_pointer`.

They are named bare here, without the `ck_<table>_` prefix: the metadata's
naming convention adds it. Passing the full name produces
`ck_claim_payment_ck_claim_payment_...`, truncated with a hash, which no
longer matches what the model declares.

Rollback: `downgrade` drops both links and the flag. The rows themselves are
untouched, so a downgrade loses the linkage but no payment history; re-running
the loader rebuilds it from RAW (PD-10).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "claim_payment",
        sa.Column("is_reversal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("claim_payment", sa.Column("reverses_claim_payment_id", sa.UUID(), nullable=True))
    op.create_index(
        "ix_claim_payment_reverses", "claim_payment", ["reverses_claim_payment_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_claim_payment_reverses_claim_payment_id_claim_payment"),
        "claim_payment",
        "claim_payment",
        ["reverses_claim_payment_id"],
        ["claim_payment_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "posting_does_not_reverse_itself",
        "claim_payment",
        "reverses_claim_payment_id <> claim_payment_id",
    )
    op.create_check_constraint(
        "only_a_reversal_reverses_a_posting",
        "claim_payment",
        "reverses_claim_payment_id IS NULL OR is_reversal",
    )

    op.add_column(
        "provider_adjustment", sa.Column("recoups_claim_payment_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_provider_adjustment_recoups_claim_payment_id_claim_payment"),
        "provider_adjustment",
        "claim_payment",
        ["recoups_claim_payment_id"],
        ["claim_payment_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_provider_adjustment_recoups_claim_payment_id_claim_payment"),
        "provider_adjustment",
        type_="foreignkey",
    )
    op.drop_column("provider_adjustment", "recoups_claim_payment_id")

    op.drop_constraint(
        op.f("ck_claim_payment_only_a_reversal_reverses_a_posting"),
        "claim_payment",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_claim_payment_posting_does_not_reverse_itself"),
        "claim_payment",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_claim_payment_reverses_claim_payment_id_claim_payment"),
        "claim_payment",
        type_="foreignkey",
    )
    op.drop_index("ix_claim_payment_reverses", table_name="claim_payment")
    op.drop_column("claim_payment", "reverses_claim_payment_id")
    op.drop_column("claim_payment", "is_reversal")
