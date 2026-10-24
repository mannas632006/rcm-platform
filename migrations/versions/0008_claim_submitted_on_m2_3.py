"""Store the business submission date on claim (M2.3).

DR-07: *"All trailing-window features MUST be computed with an as-of date
equal to the claim's submission timestamp."*

Until now the only submission-shaped timestamp in CORE was
`raw_file.received_at`, which is when **this platform archived the file** --
not when the claim was sent. The two agree in steady-state operation and
diverge exactly when the difference is most expensive: replaying a year of
history in an afternoon gives every claim the same `received_at`, so every
trailing window looks back at a period containing nothing, and every
point-in-time feature comes back null.

That is DR-07 failing silently. The features are still "computed as of the
submission timestamp" by the letter of the rule; the timestamp is just the
wrong one, and nothing in the output says so.

BHT04 -- the date the submitter stated it created the transaction -- is the
business submission date, and the 837 parser has always read it. This
migration gives it somewhere to live.

Expand only: the column is nullable, the loader fills it going forward, and
rows loaded before this migration keep a null that the mart reads as "fall
back to the archival date". Nothing needs backfilling before the next
release, and PD-10 means a full reload rebuilds it from RAW anyway.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("claim", sa.Column("submitted_on", sa.Date(), nullable=True))
    # Every trailing-window feature orders by this column within a client,
    # and M2.5's temporal split cuts on it.
    op.create_index(
        "ix_claim_submitted_on",
        "claim",
        ["client_id", "submitted_on"],
    )


def downgrade() -> None:
    op.drop_index("ix_claim_submitted_on", table_name="claim")
    op.drop_column("claim", "submitted_on")
