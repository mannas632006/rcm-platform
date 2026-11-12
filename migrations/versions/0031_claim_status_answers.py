"""Answers to claim status enquiries, and the follow-up they raise (ADR-0033).

- `claim_status_answer`: what a 277 (005010X212) said about a claim. Insert
  only by grant: what a payer said is evidence and is not rewritten.
- `work_item.kind` gains `CLAIM_STATUS_FOLLOW_UP` (EC-54, EC-56).

Expand-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
UUID = postgresql.UUID(as_uuid=True)

OLD_KINDS: tuple[str, ...] = (
    "SCRUB_EXCEPTION",
    "SUBMISSION_REJECTION",
    "DENIAL",
    "PAYMENT_EXCEPTION",
    "ELIGIBILITY_PROBLEM",
    "AUTH_PROBLEM",
    "PAYER_MATCH_REVIEW",
    "DATA_QUALITY",
    "RULE_CONFLICT",
    "MODEL_LOW_CONFIDENCE",
    "EXTERNAL_SYSTEM_FAILURE",
    "COMPLIANCE_CONCERN",
    "DEADLINE_RISK",
    "MISSING_FILING_DEADLINE",
    "CLAIM_STEP_REVIEW",
)
#: Kept in step with `rcm.work.models.WorkItemKind`.
KINDS: tuple[str, ...] = (*OLD_KINDS, "CLAIM_STATUS_FOLLOW_UP")

ANSWERS: tuple[str, ...] = (
    "ACKNOWLEDGED",
    "FINALIZED",
    "PAID",
    "DENIED",
    "NO_PAYMENT",
    "PENDING",
    "INFO_REQUESTED",
    "NOT_FOUND",
    "RETURNED",
    "NO_ANSWER",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "claim_status_answer",
        sa.Column("answer_id", UUID, nullable=False),
        sa.Column("client_id", UUID, nullable=False),
        sa.Column("raw_file_id", UUID, nullable=False),
        sa.Column("claim_id", sa.String(80), nullable=False),
        sa.Column("payer_claim_number", sa.String(80), server_default="", nullable=False),
        sa.Column("answer", sa.String(16), nullable=False),
        sa.Column("category_code", sa.String(8), nullable=False),
        sa.Column("status_code", sa.String(8), server_default="", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("payment_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"answer IN ({_quoted(ANSWERS)})",
            name=op.f("ck_claim_status_answer_status_answer_is_known"),
        ),
        sa.CheckConstraint(
            "length(claim_id) > 0",
            name=op.f("ck_claim_status_answer_status_answer_names_its_claim"),
        ),
        sa.CheckConstraint(
            "length(category_code) > 0",
            name=op.f("ck_claim_status_answer_status_answer_names_its_category"),
        ),
        sa.ForeignKeyConstraint(
            ["raw_file_id"],
            ["raw_file.raw_file_id"],
            name=op.f("fk_claim_status_answer_raw_file_id_raw_file"),
        ),
        sa.PrimaryKeyConstraint("answer_id", name=op.f("pk_claim_status_answer")),
        sa.UniqueConstraint(
            "raw_file_id",
            "claim_id",
            "category_code",
            "status_code",
            name="uq_status_answer_fact",
        ),
    )
    op.create_index(
        "ix_status_answer_client_claim",
        "claim_status_answer",
        ["client_id", "claim_id", "received_at"],
    )
    op.execute(f"REVOKE ALL ON TABLE claim_status_answer FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE claim_status_answer TO {APP_ROLE}")

    op.drop_constraint("work_item_kind_is_known", "work_item", type_="check")
    op.create_check_constraint(
        "work_item_kind_is_known", "work_item", f"kind IN ({_quoted(KINDS)})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT count(*) FROM work_item WHERE kind = 'CLAIM_STATUS_FOLLOW_UP'")
    ).scalar_one():
        raise RuntimeError("downgrade refused: status follow-up work items exist")
    if bind.execute(sa.text("SELECT count(*) FROM claim_status_answer")).scalar_one():
        raise RuntimeError("downgrade refused: status answers are recorded evidence")
    op.drop_constraint("work_item_kind_is_known", "work_item", type_="check")
    op.create_check_constraint(
        "work_item_kind_is_known", "work_item", f"kind IN ({_quoted(OLD_KINDS)})"
    )
    op.drop_index("ix_status_answer_client_claim", table_name="claim_status_answer")
    op.drop_table("claim_status_answer")
