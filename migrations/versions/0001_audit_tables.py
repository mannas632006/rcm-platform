"""Audit and decision records (SRS 5.4, PD-1, PD-2, DR-06)

Revision ID: 0001
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_record",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        # `rules_engine` | `model:<name>` | `user:<id>` -- PD-1 requires knowing
        # which, because "the model decided" is not an explanation.
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column("logic_version", sa.String(length=64), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("decision_id", name=op.f("pk_decision_record")),
    )
    op.create_index(
        "ix_decision_record_entity_decided_at",
        "decision_record",
        ["entity_type", "entity_id", sa.text("decided_at DESC")],
    )

    op.create_table(
        "audit_event",
        sa.Column("event_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("before_state", postgresql.JSONB(), nullable=True),
        sa.Column("after_state", postgresql.JSONB(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_audit_event")),
    )
    op.create_index(
        "ix_audit_event_entity_occurred_at",
        "audit_event",
        ["entity_type", "entity_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_audit_event_actor_occurred_at",
        "audit_event",
        ["actor_id", sa.text("occurred_at DESC")],
    )


def downgrade() -> None:
    # Reversible only because these tables are empty in any environment where a
    # downgrade is legitimate. Dropping populated audit tables destroys the HIPAA
    # access log; the runbook for a production rollback is to restore, not to
    # downgrade past this revision.
    op.drop_index("ix_audit_event_actor_occurred_at", table_name="audit_event")
    op.drop_index("ix_audit_event_entity_occurred_at", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_index("ix_decision_record_entity_decided_at", table_name="decision_record")
    op.drop_table("decision_record")
