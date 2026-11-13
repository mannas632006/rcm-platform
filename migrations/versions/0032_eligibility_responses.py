"""Stored 271 eligibility responses (DOC-02 M3; ADR-0034).

`eligibility_response`: what a payer said about a member's coverage. No
subscriber name or date of birth: they stay in the archived file. Insert
only by grant: what a payer said is evidence.

Expand-only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rcm_app"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "eligibility_response",
        sa.Column("response_id", UUID, nullable=False),
        sa.Column("client_id", UUID, nullable=False),
        sa.Column("raw_file_id", UUID, nullable=False),
        sa.Column("transaction_control_number", sa.String(32), nullable=False),
        sa.Column("payer_id", sa.String(80), nullable=False),
        sa.Column("member_id", sa.String(80), nullable=False),
        sa.Column("answered", sa.Boolean(), nullable=False),
        sa.Column("benefits", JSONB, nullable=False),
        sa.Column("problems", JSONB, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["raw_file_id"],
            ["raw_file.raw_file_id"],
            name=op.f("fk_eligibility_response_raw_file_id_raw_file"),
        ),
        sa.PrimaryKeyConstraint("response_id", name=op.f("pk_eligibility_response")),
    )
    op.create_index(
        "ix_eligibility_member",
        "eligibility_response",
        ["client_id", "payer_id", "member_id", "received_at"],
    )
    op.execute(f"REVOKE ALL ON TABLE eligibility_response FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE eligibility_response TO {APP_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM eligibility_response")).scalar_one():
        raise RuntimeError("downgrade refused: eligibility responses are recorded evidence")
    op.drop_index("ix_eligibility_member", table_name="eligibility_response")
    op.drop_table("eligibility_response")
