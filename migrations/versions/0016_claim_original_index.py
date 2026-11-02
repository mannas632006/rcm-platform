"""Index `claim` on the claim each submission replaces (ADR-0006 review).

Claim progression asks "has a later submission replaced this one" for every
claim on every run. Without an index each ask scans the client's claims, and
a run over 34,773 claims spent most of its time there.

Partial, because most claims are originals and never appear in the lookup.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_claim_original",
        "claim",
        ["client_id", "original_claim_id"],
        postgresql_where=sa.text("original_claim_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_claim_original", table_name="claim")
