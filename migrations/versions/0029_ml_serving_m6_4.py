"""Serving: deployments, predictions and monitor runs (M6.4, M6.6-M6.8, ADR-0028).

Three tables, each insert-only by grant, because each is evidence:

- `ml_deployment`: every change of a model version's status in service. The
  current status is the latest row; an auto-revert (FR-ML-08) is a row like
  any other, with who and why.
- `ml_prediction`: one row per line per model version: the score, or why
  there was none (FR-ML-06). The inputs and SHAP drivers are in the
  `decision_record` it points at (FR-ML-05).
- `ml_monitor_run`: what each drift check measured and what it did.

Expand-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TABLES = ("ml_deployment", "ml_prediction", "ml_monitor_run")


def _when(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "ml_deployment",
        sa.Column("deployment_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("model_name", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.String(128), nullable=False),
        _when("changed_at"),
        sa.CheckConstraint(
            "status IN ('SHADOW', 'ACTIVE', 'REVERTED', 'RETIRED')",
            name=op.f("ck_ml_deployment_status_known"),
        ),
        sa.CheckConstraint("length(trim(reason)) > 0", name=op.f("ck_ml_deployment_reason_given")),
        sa.CheckConstraint(
            "length(trim(changed_by)) > 0", name=op.f("ck_ml_deployment_changed_by_named")
        ),
        sa.ForeignKeyConstraint(
            ["model_name", "version"],
            ["ml_model_version.model_name", "ml_model_version.version"],
            name=op.f("fk_ml_deployment_model_name_ml_model_version"),
        ),
        sa.PrimaryKeyConstraint("deployment_id", name=op.f("pk_ml_deployment")),
    )
    op.create_index("ix_ml_deployment_model_name", "ml_deployment", ["model_name", "deployment_id"])

    op.create_table(
        "ml_prediction",
        sa.Column("prediction_id", UUID, nullable=False),
        sa.Column("model_version_id", UUID, nullable=False),
        sa.Column("model_name", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("client_id", UUID, nullable=False),
        sa.Column("claim_id", UUID, nullable=False),
        sa.Column("service_line_id", UUID, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decision_id", UUID, nullable=True),
        _when("predicted_at"),
        sa.CheckConstraint(
            "mode IN ('SHADOW', 'ACTIVE')", name=op.f("ck_ml_prediction_mode_known")
        ),
        sa.CheckConstraint(
            "status IN ('SCORED', 'DEGRADED')", name=op.f("ck_ml_prediction_status_known")
        ),
        # A score comes with its decision record (PD-1); a degraded request
        # has neither, and says why (FR-ML-06).
        sa.CheckConstraint(
            "(status = 'SCORED' AND score IS NOT NULL AND score >= 0 AND score <= 1 "
            "AND decision_id IS NOT NULL AND service_line_id IS NOT NULL) "
            "OR (status = 'DEGRADED' AND score IS NULL AND reason IS NOT NULL)",
            name=op.f("ck_ml_prediction_scored_or_explained"),
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["ml_model_version.model_version_id"],
            name=op.f("fk_ml_prediction_model_version_id_ml_model_version"),
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["decision_record.decision_id"],
            name=op.f("fk_ml_prediction_decision_id_decision_record"),
        ),
        sa.PrimaryKeyConstraint("prediction_id", name=op.f("pk_ml_prediction")),
        sa.UniqueConstraint(
            "model_version_id",
            "service_line_id",
            name=op.f("uq_ml_prediction_model_version_id_service_line_id"),
        ),
    )
    op.create_index(
        "ix_ml_prediction_version_predicted_at",
        "ml_prediction",
        ["model_version_id", "predicted_at"],
    )
    op.create_index("ix_ml_prediction_claim_id", "ml_prediction", ["claim_id"])

    op.create_table(
        "ml_monitor_run",
        sa.Column("monitor_run_id", UUID, nullable=False),
        sa.Column("model_name", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("thresholds_version", sa.Integer(), nullable=False),
        sa.Column("metrics", JSONB, nullable=False),
        sa.Column("breaches", JSONB, nullable=False),
        _when("ran_at"),
        sa.CheckConstraint(
            "severity IN ('OK', 'WARNING', 'CRITICAL')",
            name=op.f("ck_ml_monitor_run_severity_known"),
        ),
        sa.CheckConstraint(
            "action IN ('NONE', 'REVERTED')", name=op.f("ck_ml_monitor_run_action_known")
        ),
        sa.PrimaryKeyConstraint("monitor_run_id", name=op.f("pk_ml_monitor_run")),
    )

    for table in TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM ml_deployment)
               OR EXISTS (SELECT 1 FROM ml_prediction)
               OR EXISTS (SELECT 1 FROM ml_monitor_run) THEN
                RAISE EXCEPTION 'serving records exist; they cannot be dropped';
            END IF;
        END $$;
        """
    )
    for table in reversed(TABLES):
        op.drop_table(table)
