"""Queue prioritisation: why an item ranks where it does (M3.6).

    PD-15: Timely-filing deadline overrides all other prioritisation.
    FR-WORK-05 (MUST): Deadline-pinned items bypass the scoring model.
    FR-WORK-06 (SHOULD): The queue exposes why an item ranks where it does.

- `priority_reason`, `priority_components`, `priority_logic_version` and
  `prioritised_at` record the explanation beside the score.
- `bulk_handling` routes low-value work away from individual queues
  (BR-M10-06, [TBD-46]).
- `only_a_pinned_item_scores_100` makes 100 mean pinned and only pinned;
  `a_pinned_item_is_not_bulk` keeps an urgent claim in a person's queue.
- `ix_work_item_next` serves the queue read: pinned, score, SLA, age.

Existing rows are reconciled before the CHECK is added: a pinned row is
set to 100, and an unpinned 100 is capped at 99.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_item",
        sa.Column("priority_reason", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "work_item",
        sa.Column("priority_components", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "work_item", sa.Column("priority_logic_version", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "work_item", sa.Column("prioritised_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "work_item",
        sa.Column("bulk_handling", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("work_item", sa.Column("days_to_deadline", sa.Integer(), nullable=True))

    op.execute("UPDATE work_item SET priority_score = 100 WHERE deadline_pinned")
    op.execute(
        "UPDATE work_item SET priority_score = 99 "
        "WHERE NOT deadline_pinned AND priority_score = 100"
    )
    op.create_check_constraint(
        "only_a_pinned_item_scores_100", "work_item", "deadline_pinned = (priority_score = 100)"
    )
    op.create_check_constraint(
        "a_pinned_item_is_not_bulk", "work_item", "NOT (deadline_pinned AND bulk_handling)"
    )
    op.create_index(
        "ix_work_item_next",
        "work_item",
        ["client_id", "state", "bulk_handling", "deadline_pinned", "priority_score"],
    )


def downgrade() -> None:
    op.drop_index("ix_work_item_next", table_name="work_item")
    op.drop_constraint("a_pinned_item_is_not_bulk", "work_item", type_="check")
    op.drop_constraint("only_a_pinned_item_scores_100", "work_item", type_="check")
    op.drop_column("work_item", "days_to_deadline")
    op.drop_column("work_item", "bulk_handling")
    op.drop_column("work_item", "prioritised_at")
    op.drop_column("work_item", "priority_logic_version")
    op.drop_column("work_item", "priority_components")
    op.drop_column("work_item", "priority_reason")
