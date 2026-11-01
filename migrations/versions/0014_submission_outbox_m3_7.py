"""The submission outbox (M3.7, IR-06, IR-07).

    IR-06 (MUST): Submission MUST use the transactional outbox pattern.
    IR-07 (MUST): Every submission carries an idempotency key.

One row per file to transmit, written in the same transaction that moves
the claim to `Submitted`. `(client_id, idempotency_key)` is unique, so a
retried enqueue cannot create a second submission (EC-41).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submission_outbox",
        sa.Column("outbox_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=64), server_default="", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_via", sa.String(length=64), nullable=True),
        sa.Column("receipt_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SENT')",
            name=op.f("ck_submission_outbox_outbox_status_is_known"),
        ),
        sa.CheckConstraint(
            "(status = 'SENT') = "
            "(sent_at IS NOT NULL AND sent_via IS NOT NULL AND receipt_id IS NOT NULL)",
            name=op.f("ck_submission_outbox_a_sent_row_records_its_receipt"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_submission_outbox_outbox_attempts_are_counted")
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0", name=op.f("ck_submission_outbox_outbox_has_a_key")
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.claim_id"], name=op.f("fk_submission_outbox_claim_id_claim")
        ),
        sa.PrimaryKeyConstraint("outbox_id", name=op.f("pk_submission_outbox")),
        sa.UniqueConstraint("client_id", "idempotency_key", name=op.f("uq_outbox_idempotency_key")),
    )
    op.create_index(
        "ix_outbox_due", "submission_outbox", ["client_id", "status", "next_attempt_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT count(*) FROM submission_outbox WHERE status = 'PENDING'")
    ).scalar_one():
        raise RuntimeError("downgrade refused: unsent submissions are waiting in the outbox")
    op.drop_index("ix_outbox_due", table_name="submission_outbox")
    op.drop_table("submission_outbox")
