"""The claim state machine (M3.1, M3.2).

    FR-BE-09 (MUST): Valid transitions are declared as data, not scattered
    through `if` statements. Undefined transitions are rejected.

    BR-STATE-01 (MUST): Every state transition MUST be logged with
    timestamp, actor, and reason.

Two things arrive together because neither is useful alone: the claim gains
a state, and `claim_state_transition` gains the history of how it got there.

**Why `state` is text with a CHECK rather than a native enum.** The state
list is business vocabulary and will change -- M4 may well split `Denied`,
and a payer-specific workflow could add one. Adding a value to a Postgres
enum is cheap but *removing* or renaming one is not, and an `ALTER TYPE`
takes a lock on every table that uses it. A CHECK constraint is altered
without touching the rows. The application-level guarantee is in
`rcm.domain.claim_state`; this constraint is what stops a backfill or a
hand-edited row introducing a state the machine has never heard of.

**The CHECK is written by hand.** Alembic does not autogenerate CHECK
constraints on ALTER, so a constraint added to the model and not written
here would simply not exist in the database -- the same trap M1.10 found.

**`state` is `NOT NULL` with a server default.** Every claim already in the
table predates the state machine and is given `Registered`, which is
truthful: it is where a claim starts, and nothing has moved it since. The
alternative, a nullable column, would mean every query reading state has to
decide what null means, and they would not all decide the same thing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept in step with `rcm.domain.claim_state.ClaimState`, and asserted to be
#: by `tests/security/test_claim_state_constraints.py` -- a list in two
#: places is a list that drifts unless something compares them.
STATES: tuple[str, ...] = (
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


def upgrade() -> None:
    op.create_table(
        "claim_state_transition",
        sa.Column("transition_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("source_state", sa.String(length=32), nullable=False),
        sa.Column("target_state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("guard", sa.String(length=48), server_default="", nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("next_action_due", sa.Date(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(actor_id) > 0",
            name=op.f("ck_claim_state_transition_transition_names_its_actor"),
        ),
        sa.CheckConstraint(
            "length(reason) > 0",
            name=op.f("ck_claim_state_transition_transition_states_a_reason"),
        ),
        sa.CheckConstraint(
            "length(source_state) > 0",
            name=op.f("ck_claim_state_transition_transition_names_its_source"),
        ),
        sa.CheckConstraint(
            "length(target_state) > 0",
            name=op.f("ck_claim_state_transition_transition_names_its_target"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claim.claim_id"],
            name=op.f("fk_claim_state_transition_claim_id_claim"),
        ),
        sa.PrimaryKeyConstraint("transition_id", name=op.f("pk_claim_state_transition")),
    )
    op.create_index(
        "ix_claim_transition_claim",
        "claim_state_transition",
        ["client_id", "claim_id", "occurred_at"],
    )
    op.create_index(
        "ix_claim_transition_target", "claim_state_transition", ["client_id", "target_state"]
    )

    op.add_column(
        "claim",
        sa.Column("state", sa.String(length=32), server_default="Registered", nullable=False),
    )
    op.add_column("claim", sa.Column("next_action_due", sa.Date(), nullable=True))
    op.create_index("ix_claim_next_action", "claim", ["client_id", "state", "next_action_due"])

    # Written by hand: Alembic does not autogenerate CHECK constraints on
    # ALTER, so this would otherwise live in the model and nowhere else.
    quoted = ", ".join(f"'{state}'" for state in STATES)
    op.create_check_constraint("state_is_known", "claim", f"state IN ({quoted})")


def downgrade() -> None:
    # The bare name: the metadata naming convention adds the `ck_claim_`
    # prefix, and passing the already-prefixed name asks Postgres to drop
    # `ck_claim_ck_claim_state_is_known`. Same trap M1.10 found.
    op.drop_constraint("state_is_known", "claim", type_="check")
    op.drop_index("ix_claim_next_action", table_name="claim")
    op.drop_column("claim", "next_action_due")
    op.drop_column("claim", "state")
    op.drop_index("ix_claim_transition_target", table_name="claim_state_transition")
    op.drop_index("ix_claim_transition_claim", table_name="claim_state_transition")
    op.drop_table("claim_state_transition")
