"""The model registry: one row per trained model version (M6.1, ADR-0026).

Insert-only by grant. FR-ML-03 asks that every model version record its data
hash, code commit, hyperparameters and evaluation; that is only worth asking
if the record cannot be rewritten after the model is in use. Expand-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "ml_model_version",
        sa.Column("model_version_id", UUID, nullable=False),
        sa.Column("model_name", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("data_source", sa.String(16), nullable=False),
        sa.Column("data_hash", sa.String(64), nullable=False),
        sa.Column("code_commit", sa.String(40), nullable=False),
        sa.Column("code_dirty", sa.Boolean(), nullable=False),
        sa.Column("thresholds_version", sa.Integer(), nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("metrics", JSONB, nullable=False),
        sa.Column("baseline", JSONB, nullable=False),
        sa.Column("windows", JSONB, nullable=False),
        sa.Column("encoding", JSONB, nullable=False),
        sa.Column("artefact", sa.Text(), nullable=False),
        sa.Column("mlflow_run_id", sa.String(64), nullable=True),
        sa.Column("trained_by", sa.String(128), nullable=False),
        sa.Column(
            "trained_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verdict IN ('ACCEPTED', 'REJECTED')", name=op.f("ck_ml_model_version_verdict_known")
        ),
        sa.CheckConstraint(
            "data_source IN ('REAL', 'SYNTHETIC')",
            name=op.f("ck_ml_model_version_data_source_known"),
        ),
        sa.CheckConstraint(
            "char_length(data_hash) = 64", name=op.f("ck_ml_model_version_data_hash_is_sha256")
        ),
        sa.PrimaryKeyConstraint("model_version_id", name=op.f("pk_ml_model_version")),
        sa.UniqueConstraint(
            "model_name", "version", name=op.f("uq_ml_model_version_model_name_version")
        ),
    )
    op.execute(f"REVOKE ALL ON TABLE ml_model_version FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE ml_model_version TO {APP_ROLE}")


def downgrade() -> None:
    # A registered model may have made predictions; its record is what those
    # predictions' decision records point at.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM ml_model_version) THEN
                RAISE EXCEPTION 'model versions are recorded; they cannot be dropped';
            END IF;
        END $$;
        """
    )
    op.drop_table("ml_model_version")
