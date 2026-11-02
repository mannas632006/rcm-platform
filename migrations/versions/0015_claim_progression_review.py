"""Automatic claim progression with the submitter's review (ADR-0006).

- `claim.state` gains `Replaced`: a submission superseded by a corrected one.
- `claim_state_transition` gains a review: status, reviewer, time, note.
  System steps wait for the submitter (owner decision, 2026-11-01).
- `work_item.kind` gains `CLAIM_STEP_REVIEW`.
- `claim_state_transition` gains `sequence`: the order of steps taken in one
  transaction, which share `occurred_at`.

Existing transitions are NOT_REQUIRED: they were taken before review
existed, and pretending they were reviewed would be false.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATES: tuple[str, ...] = (
    "Registered",
    "EligibilityVerified",
    "AuthPending",
    "AuthApproved",
    "AuthDenied",
    "ServiceRendered",
    "Coded",
    "Charged",
    "Scrubbing",
    "ScrubFailed",
    "ReadyToSubmit",
    "Submitted",
    "Rejected",
    "InAdjudication",
    "Paid",
    "PartiallyPaid",
    "Denied",
    "Appealing",
    "Correcting",
    "WrittenOff",
    "SecondaryBilling",
    "PatientBalance",
    "Closed",
    "Reopened",
    "Cancelled",
)
#: Kept in step with `rcm.domain.claim_state.ClaimState`.
STATES: tuple[str, ...] = (*OLD_STATES, "Replaced")

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
)
#: Kept in step with `rcm.work.models.WorkItemKind`.
KINDS: tuple[str, ...] = (*OLD_KINDS, "CLAIM_STEP_REVIEW")


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # Existing rows are numbered in physical order; their order within one
    # transaction was never recorded, so nothing better is available.
    op.add_column(
        "claim_state_transition",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=True), nullable=False),
    )
    op.drop_constraint("state_is_known", "claim", type_="check")
    op.create_check_constraint("state_is_known", "claim", f"state IN ({_quoted(STATES)})")

    op.add_column(
        "claim_state_transition",
        sa.Column(
            "review_status", sa.String(length=16), server_default="NOT_REQUIRED", nullable=False
        ),
    )
    op.add_column(
        "claim_state_transition", sa.Column("reviewed_by", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "claim_state_transition",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "claim_state_transition",
        sa.Column("review_note", sa.Text(), server_default="", nullable=False),
    )
    op.create_check_constraint(
        "transition_review_status_is_known",
        "claim_state_transition",
        "review_status IN ('NOT_REQUIRED','PENDING','APPROVED','SENT_BACK')",
    )
    op.create_check_constraint(
        "a_reviewed_step_names_its_reviewer",
        "claim_state_transition",
        "(review_status IN ('APPROVED','SENT_BACK')) = "
        "(reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "a_step_sent_back_says_why",
        "claim_state_transition",
        "review_status <> 'SENT_BACK' OR length(review_note) > 0",
    )
    op.create_index(
        "ix_claim_transition_review",
        "claim_state_transition",
        ["client_id", "claim_id", "review_status"],
    )

    op.drop_constraint("work_item_kind_is_known", "work_item", type_="check")
    op.create_check_constraint(
        "work_item_kind_is_known", "work_item", f"kind IN ({_quoted(KINDS)})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM claim WHERE state = 'Replaced'")).scalar_one():
        raise RuntimeError("downgrade refused: claims are in the Replaced state")
    if bind.execute(
        sa.text("SELECT count(*) FROM work_item WHERE kind = 'CLAIM_STEP_REVIEW'")
    ).scalar_one():
        raise RuntimeError("downgrade refused: claim step reviews exist")

    op.drop_constraint("work_item_kind_is_known", "work_item", type_="check")
    op.create_check_constraint(
        "work_item_kind_is_known", "work_item", f"kind IN ({_quoted(OLD_KINDS)})"
    )
    op.drop_index("ix_claim_transition_review", table_name="claim_state_transition")
    op.drop_constraint("a_step_sent_back_says_why", "claim_state_transition", type_="check")
    op.drop_constraint(
        "a_reviewed_step_names_its_reviewer", "claim_state_transition", type_="check"
    )
    op.drop_constraint("transition_review_status_is_known", "claim_state_transition", type_="check")
    op.drop_column("claim_state_transition", "review_note")
    op.drop_column("claim_state_transition", "reviewed_at")
    op.drop_column("claim_state_transition", "reviewed_by")
    op.drop_column("claim_state_transition", "review_status")
    op.drop_column("claim_state_transition", "sequence")
    op.drop_constraint("state_is_known", "claim", type_="check")
    op.create_check_constraint("state_is_known", "claim", f"state IN ({_quoted(OLD_STATES)})")
