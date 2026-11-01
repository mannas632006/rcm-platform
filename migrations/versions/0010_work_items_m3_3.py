"""Work items: the queue a person actually works (M3.3, M3.4).

    FR-WORK-01 (MUST): Assignment places an exclusive lock. Two operators
    must never work one item.

    FR-WORK-02 (MUST): Assignment locks expire after `[TBD-57]` minutes of
    inactivity and return the item to the queue.

The constraints are where FR-WORK-01 and FR-WORK-04 actually live. Two of
them are written as equivalences rather than implications, which is the
part worth reading twice:

    (state IN ('Assigned','InProgress')) = (assigned_to IS NOT NULL
                                            AND lock_expires_at IS NOT NULL)

Both directions matter. A held item without a holder is a lock nobody can
see; a holder on an item nobody is holding is a lease that never expires,
and the sweep will not touch it because its state says it is in the queue.
An implication in either direction alone permits one of those.

    (state IN ('Resolved','AutoResolved')) = (outcome IS NOT NULL)

Likewise: a resolved item with no outcome is the row FR-WORK-04 forbids,
and an outcome on an unresolved item is a conclusion recorded before the
work was done.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('work_item',
    sa.Column('work_item_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.UUID(), nullable=False),
    sa.Column('claim_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('state', sa.String(length=16), server_default='Created', nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('detail', sa.Text(), server_default='', nullable=False),
    sa.Column('priority_score', sa.Integer(), nullable=False),
    sa.Column('deadline_pinned', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('assigned_to', sa.String(length=128), nullable=True),
    sa.Column('lock_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.String(length=32), nullable=True),
    sa.Column('resolution_note', sa.Text(), server_default='', nullable=False),
    sa.Column('owner_role', sa.String(length=48), nullable=False),
    sa.Column('sla_due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(state IN ('Assigned','InProgress')) = (assigned_to IS NOT NULL AND lock_expires_at IS NOT NULL)", name=op.f('ck_work_item_a_held_item_names_its_holder_and_its_lease')),
    sa.CheckConstraint("(state IN ('Resolved','AutoResolved')) = (outcome IS NOT NULL)", name=op.f('ck_work_item_a_resolved_item_records_its_outcome')),
    sa.CheckConstraint("state IN ('Created','Queued','Assigned','InProgress','Escalated','Resolved','Reopened','AutoResolved')", name=op.f('ck_work_item_work_item_state_is_known')),
    sa.CheckConstraint('length(owner_role) > 0', name=op.f('ck_work_item_work_item_names_its_owner')),
    sa.CheckConstraint('length(title) > 0', name=op.f('ck_work_item_work_item_names_its_problem')),
    sa.CheckConstraint('priority_score BETWEEN 0 AND 100', name=op.f('ck_work_item_priority_score_is_a_percentage')),
    sa.ForeignKeyConstraint(['claim_id'], ['claim.claim_id'], name=op.f('fk_work_item_claim_id_claim')),
    sa.PrimaryKeyConstraint('work_item_id', name=op.f('pk_work_item'))
    )
    op.create_index('ix_work_item_claim', 'work_item', ['client_id', 'claim_id'], unique=False)
    op.create_index('ix_work_item_lease', 'work_item', ['client_id', 'lock_expires_at'], unique=False)
    op.create_index('ix_work_item_queue', 'work_item', ['client_id', 'state', 'priority_score'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_work_item_queue', table_name='work_item')
    op.drop_index('ix_work_item_lease', table_name='work_item')
    op.drop_index('ix_work_item_claim', table_name='work_item')
    op.drop_table('work_item')
