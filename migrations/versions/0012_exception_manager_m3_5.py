"""The exception manager: routing, SLAs and compliance suspensions (M3.5).

    BR-EXC-02 (MUST): Every exception MUST have an owner and an SLA.
    BR-EXC-03 (MUST): Compliance-category exceptions MUST suspend the
    affected automation path until reviewed.

- `work_item.category` (DOC-02 section 10.1's routing class) is NOT NULL.
  Existing rows are backfilled from their kind.
- `work_item.sla_due_at` becomes NOT NULL. Existing rows are given their
  creation time plus 72 hours, the provisional operational SLA.
- `work_item.source_key` plus a partial unique index make raising the same
  fact twice return the open item rather than a duplicate.
- The kind CHECK gains the six exception kinds.
- `automation_suspension` records which automation path a compliance
  exception stopped, and who lifted it after review.

Expand, backfill, then tighten, all in one revision: `work_item` has held
no production data.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS: tuple[str, ...] = (
    "SCRUB_EXCEPTION",
    "SUBMISSION_REJECTION",
    "DENIAL",
    "PAYMENT_EXCEPTION",
    "ELIGIBILITY_PROBLEM",
    "AUTH_PROBLEM",
    "PAYER_MATCH_REVIEW",
    "DATA_QUALITY",
)

#: Kept in step with `rcm.work.models.WorkItemKind`.
KINDS: tuple[str, ...] = (
    *OLD_KINDS,
    "RULE_CONFLICT",
    "MODEL_LOW_CONFIDENCE",
    "EXTERNAL_SYSTEM_FAILURE",
    "COMPLIANCE_CONCERN",
    "DEADLINE_RISK",
    "MISSING_FILING_DEADLINE",
)

#: Kept in step with `rcm.work.models.ExceptionCategory`.
CATEGORIES: tuple[str, ...] = (
    "DATA_QUALITY",
    "RULE_CONFLICT",
    "MODEL_LOW_CONFIDENCE",
    "EXTERNAL_SYSTEM",
    "COMPLIANCE",
    "DEADLINE_RISK",
    "OPERATIONAL",
)

#: Kept in step with `rcm.work.models.AutomationPath`.
PATHS: tuple[str, ...] = (
    "CLAIM_PROGRESSION",
    "CLAIM_SUBMISSION",
    "AUTO_CORRECTION",
    "AUTO_RESOLUTION",
    "REMITTANCE_POSTING",
)

OPEN_STATES = "('Created','Queued','Assigned','InProgress','Escalated','Reopened')"


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.add_column("work_item", sa.Column("category", sa.String(length=24), nullable=True))
    op.add_column("work_item", sa.Column("source_key", sa.String(length=160), nullable=True))
    op.execute(
        "UPDATE work_item SET category = CASE "
        "WHEN kind IN ('DATA_QUALITY','PAYER_MATCH_REVIEW') THEN 'DATA_QUALITY' "
        "ELSE 'OPERATIONAL' END"
    )
    op.execute(
        "UPDATE work_item SET sla_due_at = created_at + interval '72 hours' "
        "WHERE sla_due_at IS NULL"
    )
    op.alter_column("work_item", "category", nullable=False)
    op.alter_column("work_item", "sla_due_at", nullable=False)

    op.create_check_constraint(
        "work_item_category_is_known", "work_item", f"category IN ({_quoted(CATEGORIES)})"
    )
    op.drop_constraint("work_item_kind_is_known", "work_item", type_="check")
    op.create_check_constraint(
        "work_item_kind_is_known", "work_item", f"kind IN ({_quoted(KINDS)})"
    )
    op.create_index(
        "uq_work_item_open_source",
        "work_item",
        ["client_id", "kind", "source_key"],
        unique=True,
        postgresql_where=sa.text(f"source_key IS NOT NULL AND state IN {OPEN_STATES}"),
    )

    op.create_table(
        "automation_suspension",
        sa.Column("suspension_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("path", sa.String(length=32), nullable=False),
        sa.Column("work_item_id", sa.UUID(), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_by", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            f"path IN ({_quoted(PATHS)})",
            name=op.f("ck_automation_suspension_suspension_path_is_known"),
        ),
        sa.CheckConstraint(
            "(lifted_at IS NULL) = (lifted_by IS NULL) "
            "AND (lifted_by IS NULL OR length(lifted_by) > 0)",
            name=op.f("ck_automation_suspension_a_lift_names_who_lifted_it"),
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_item.work_item_id"],
            name=op.f("fk_automation_suspension_work_item_id_work_item"),
        ),
        sa.PrimaryKeyConstraint("suspension_id", name=op.f("pk_automation_suspension")),
    )
    op.create_index(
        "ix_suspension_active", "automation_suspension", ["client_id", "path", "lifted_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM automation_suspension")).scalar_one():
        raise RuntimeError(
            "downgrade refused: automation_suspension holds compliance history; archive it first"
        )
    op.drop_index("ix_suspension_active", table_name="automation_suspension")
    op.drop_table("automation_suspension")
    op.drop_index("uq_work_item_open_source", table_name="work_item")
    # Refuse rather than delete: rows of the new kinds cannot satisfy the
    # old CHECK, and dropping them would lose exceptions -- compliance
    # concerns among them -- with no record. Resolve and archive first.
    bind = op.get_bind()
    blocking = bind.execute(
        sa.text("SELECT count(*) FROM work_item WHERE kind <> ALL(:kinds)").bindparams(
            kinds=list(OLD_KINDS)
        )
    ).scalar_one()
    if blocking:
        raise RuntimeError(
            f"downgrade refused: {blocking} work items use kinds added in 0012; "
            "resolve and archive them first"
        )
    op.drop_constraint("work_item_kind_is_known", "work_item", type_="check")
    op.create_check_constraint(
        "work_item_kind_is_known", "work_item", f"kind IN ({_quoted(OLD_KINDS)})"
    )
    op.drop_constraint("work_item_category_is_known", "work_item", type_="check")
    op.alter_column("work_item", "sla_due_at", nullable=True)
    op.drop_column("work_item", "source_key")
    op.drop_column("work_item", "category")
