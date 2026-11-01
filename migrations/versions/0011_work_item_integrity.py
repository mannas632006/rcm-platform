"""Work item integrity: known kinds and outcomes, and honest automation counts.

    FR-WORK-03: AutoResolved is counted separately from human resolutions.

    FR-WORK-04 (MUST): Every resolution records a structured outcome, not
    free text.

**`first_claimed_at`.** `Queued` is where a new item waits, and also where a
released, expired or reopened item goes back to. So the state alone cannot
say whether a person ever worked an item, and `Queued -> AutoResolved` would
count a human's work as automation. The column is set on the first claim and
never cleared; a CHECK refuses an `AutoResolved` row that has one.

**`kind` and `outcome` CHECKs.** Both were enforced only by Python enums. A
backfill or a hand-written row could put free text in `outcome`, which is
the training signal FR-WORK-04 exists to keep structured.

The lists are written out here because Alembic does not autogenerate CHECK
constraints. `tests/security/test_work_item_constraints.py` compares each
live constraint with its enum, so the two cannot drift.

`work_item` has held no production data, so adding the constraints needs no
backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept in step with `rcm.work.models.WorkItemKind`.
KINDS: tuple[str, ...] = (
    "SCRUB_EXCEPTION",
    "SUBMISSION_REJECTION",
    "DENIAL",
    "PAYMENT_EXCEPTION",
    "ELIGIBILITY_PROBLEM",
    "AUTH_PROBLEM",
    "PAYER_MATCH_REVIEW",
    "DATA_QUALITY",
)

#: Kept in step with `rcm.work.state.WorkItemOutcome`.
OUTCOMES: tuple[str, ...] = (
    "CORRECTED_AND_RESUBMITTED",
    "APPEALED",
    "WRITTEN_OFF",
    "BILLED_TO_PATIENT",
    "BILLED_TO_SECONDARY",
    "PAYER_ERROR_CONFIRMED",
    "NO_ACTION_NEEDED",
    "DUPLICATE",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.add_column(
        "work_item", sa.Column("first_claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "work_item_kind_is_known", "work_item", f"kind IN ({_quoted(KINDS)})"
    )
    op.create_check_constraint(
        "work_item_outcome_is_known",
        "work_item",
        f"outcome IS NULL OR outcome IN ({_quoted(OUTCOMES)})",
    )
    op.create_check_constraint(
        "auto_resolved_was_never_held",
        "work_item",
        "state <> 'AutoResolved' OR first_claimed_at IS NULL",
    )


def downgrade() -> None:
    # Bare names: the naming convention adds the `ck_work_item_` prefix.
    op.drop_constraint("auto_resolved_was_never_held", "work_item", type_="check")
    op.drop_constraint("work_item_outcome_is_known", "work_item", type_="check")
    op.drop_constraint("work_item_kind_is_known", "work_item", type_="check")
    op.drop_column("work_item", "first_claimed_at")
