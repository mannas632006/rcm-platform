"""CORE remittance tables (M1.8)

Creates the tables an 835 loads into, and the constraints that make two prime
directives properties of the database rather than habits of the loader. A test
can be deleted and a loader rewritten in a hurry; a CHECK cannot be worked
around by code that did not think about it.

**PD-3** -- `remittance_adjustment.cas_group_code` is NOT NULL with a CHECK
against the five values in X12 code list 1033. CO-45 is a contractual
write-off the patient may never be billed for; PR-45 is an amount they owe.
A row without the group is not incomplete, it is ambiguous between two
opposite outcomes.

**PD-6** -- the grain *is* the table. `remittance_adjustment` holds one row per
CAS reason, so a line with three reasons is three rows. There is deliberately
no unique constraint on (service_line_id, reason_code): a payer can repeat a
reason with different amounts, and forbidding it would force a loader to merge
them, which is exactly the collapsing PD-6 prohibits.

**PD-8** -- `payer.payer_id` is the primary key and `payer.name` carries no
uniqueness constraint at all. A unique index on the name would be PD-8
violated in the schema.

**PD-20** -- `client_id` is on every table here rather than reached through a
join, because `scoped_query` filters the entity being queried and a scope that
depends on a join is one a future query can omit by not writing the join.

**DR-10** -- a row may only be QUARANTINED if it names the assertion it failed.
There is deliberately no PARTIALLY_LOADED status; its absence is the point.

Every NOT NULL column with a default has a **server** default. A default that
exists only in Python is a trap for every writer that is not the ORM -- a
migration, a bulk load, a psql session -- and this was found by a test fixture
inserting through raw SQL rather than in production.

Grants come from the default privileges in `infra/postgres/01-roles.sql`. The
audit tables are the exception, narrowed in 0002: append-only is a property of
specific tables, not of the schema.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 19:00:24.730287+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('payer',
    sa.Column('payer_id', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('plan_type', sa.String(length=64), nullable=True),
    sa.Column('timely_filing_days', sa.Integer(), nullable=True),
    sa.Column('last_verified_date', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('payer_id', name=op.f('pk_payer'))
    )
    op.create_table('raw_file',
    sa.Column('raw_file_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.UUID(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('byte_size', sa.BigInteger(), nullable=False),
    sa.Column('storage_path', sa.Text(), nullable=False),
    sa.Column('original_filename', sa.Text(), nullable=False),
    sa.Column('transaction_kind', sa.String(length=16), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='ARCHIVED', nullable=False),
    sa.Column('quarantine_assertion', sa.String(length=128), nullable=True),
    sa.Column('quarantine_reason', sa.Text(), nullable=True),
    sa.Column('quarantine_context', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status <> 'QUARANTINED') OR (quarantine_assertion IS NOT NULL AND quarantine_reason IS NOT NULL)", name=op.f('ck_raw_file_quarantine_names_its_assertion')),
    sa.CheckConstraint("status IN ('ARCHIVED', 'LOADED', 'QUARANTINED')", name=op.f('ck_raw_file_status_is_known')),
    sa.PrimaryKeyConstraint('raw_file_id', name=op.f('pk_raw_file')),
    sa.UniqueConstraint('content_hash', name='uq_raw_file_content_hash')
    )
    op.create_index('ix_raw_file_client_received', 'raw_file', ['client_id', 'received_at'], unique=False)
    op.create_index('ix_raw_file_status', 'raw_file', ['status'], unique=False)
    op.create_table('payer_alias',
    sa.Column('alias_id', sa.UUID(), nullable=False),
    sa.Column('payer_id', sa.String(length=80), nullable=False),
    sa.Column('alias', sa.String(length=255), nullable=False),
    sa.Column('normalised_alias', sa.String(length=255), nullable=False),
    sa.Column('confirmed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['payer_id'], ['payer.payer_id'], name=op.f('fk_payer_alias_payer_id_payer'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('alias_id', name=op.f('pk_payer_alias')),
    sa.UniqueConstraint('normalised_alias', name='uq_payer_alias_normalised_alias')
    )
    op.create_index('ix_payer_alias_payer_id', 'payer_alias', ['payer_id'], unique=False)
    op.create_table('remittance',
    sa.Column('remittance_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.UUID(), nullable=False),
    sa.Column('raw_file_id', sa.UUID(), nullable=False),
    sa.Column('payer_id', sa.String(length=80), nullable=False),
    sa.Column('payment_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('payment_date', sa.Date(), nullable=True),
    sa.Column('trace_number', sa.String(length=80), server_default='', nullable=False),
    sa.Column('transaction_control_number', sa.String(length=80), server_default='', nullable=False),
    sa.Column('payee_npi', sa.String(length=20), server_default='', nullable=False),
    sa.Column('claim_payment_total', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('provider_adjustment_total', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['payer_id'], ['payer.payer_id'], name=op.f('fk_remittance_payer_id_payer'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['raw_file_id'], ['raw_file.raw_file_id'], name=op.f('fk_remittance_raw_file_id_raw_file'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('remittance_id', name=op.f('pk_remittance')),
    sa.UniqueConstraint('raw_file_id', 'transaction_control_number', name='uq_remittance_raw_file_id_transaction_control_number')
    )
    op.create_index('ix_remittance_client_payer', 'remittance', ['client_id', 'payer_id'], unique=False)
    op.create_index('ix_remittance_payment_date', 'remittance', ['payment_date'], unique=False)
    op.create_table('claim_payment',
    sa.Column('claim_payment_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.UUID(), nullable=False),
    sa.Column('remittance_id', sa.UUID(), nullable=False),
    sa.Column('claim_id', sa.String(length=80), nullable=False),
    sa.Column('payer_control_number', sa.String(length=80), server_default='', nullable=False),
    sa.Column('status_code', sa.String(length=8), nullable=False),
    sa.Column('filing_indicator', sa.String(length=8), server_default='', nullable=False),
    sa.Column('total_charge', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('total_paid', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('patient_responsibility', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('service_date', sa.Date(), nullable=True),
    sa.Column('member_id', sa.String(length=80), server_default='', nullable=False),
    sa.Column('rendering_provider_npi', sa.String(length=20), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['remittance_id'], ['remittance.remittance_id'], name=op.f('fk_claim_payment_remittance_id_remittance'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('claim_payment_id', name=op.f('pk_claim_payment'))
    )
    op.create_index('ix_claim_payment_client_claim', 'claim_payment', ['client_id', 'claim_id'], unique=False)
    op.create_index('ix_claim_payment_payer_control', 'claim_payment', ['payer_control_number'], unique=False)
    op.create_index('ix_claim_payment_remittance', 'claim_payment', ['remittance_id'], unique=False)
    op.create_table('provider_adjustment',
    sa.Column('provider_adjustment_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.UUID(), nullable=False),
    sa.Column('remittance_id', sa.UUID(), nullable=False),
    sa.Column('provider_identifier', sa.String(length=80), server_default='', nullable=False),
    sa.Column('fiscal_period_end', sa.Date(), nullable=True),
    sa.Column('reason_code', sa.String(length=16), server_default='', nullable=False),
    sa.Column('reference', sa.String(length=80), server_default='', nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('sequence', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['remittance_id'], ['remittance.remittance_id'], name=op.f('fk_provider_adjustment_remittance_id_remittance'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('provider_adjustment_id', name=op.f('pk_provider_adjustment'))
    )
    op.create_index('ix_provider_adjustment_reference', 'provider_adjustment', ['reference'], unique=False)
    op.create_index('ix_provider_adjustment_remittance', 'provider_adjustment', ['remittance_id'], unique=False)
    op.create_table('remittance_service_line',
    sa.Column('service_line_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.UUID(), nullable=False),
    sa.Column('claim_payment_id', sa.UUID(), nullable=False),
    sa.Column('line_number', sa.Integer(), nullable=False),
    sa.Column('procedure_code', sa.String(length=16), nullable=False),
    sa.Column('modifiers', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('charge_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('paid_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('allowed_amount', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('units', sa.Numeric(precision=12, scale=3), server_default='1', nullable=False),
    sa.Column('service_date', sa.Date(), nullable=True),
    sa.Column('remark_codes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['claim_payment_id'], ['claim_payment.claim_payment_id'], name=op.f('fk_remittance_service_line_claim_payment_id_claim_payment'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('service_line_id', name=op.f('pk_remittance_service_line')),
    sa.UniqueConstraint('claim_payment_id', 'line_number', name='uq_remittance_service_line_claim_line')
    )
    op.create_index('ix_remittance_service_line_client_procedure', 'remittance_service_line', ['client_id', 'procedure_code'], unique=False)
    op.create_table('remittance_adjustment',
    sa.Column('adjustment_id', sa.UUID(), nullable=False),
    sa.Column('client_id', sa.UUID(), nullable=False),
    sa.Column('claim_payment_id', sa.UUID(), nullable=False),
    sa.Column('service_line_id', sa.UUID(), nullable=True),
    sa.Column('cas_group_code', sa.String(length=2), nullable=False),
    sa.Column('reason_code', sa.String(length=8), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('claim_level', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('sequence', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("cas_group_code IN ('CO', 'PR', 'OA', 'PI', 'CR')", name=op.f('ck_remittance_adjustment_cas_group_code_is_valid')),
    sa.CheckConstraint('(claim_level AND service_line_id IS NULL) OR (NOT claim_level AND service_line_id IS NOT NULL)', name=op.f('ck_remittance_adjustment_claim_level_adjustments_have_no_service_line')),
    sa.CheckConstraint('length(reason_code) > 0', name=op.f('ck_remittance_adjustment_reason_code_is_present')),
    sa.ForeignKeyConstraint(['claim_payment_id'], ['claim_payment.claim_payment_id'], name=op.f('fk_remittance_adjustment_claim_payment_id_claim_payment'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['service_line_id'], ['remittance_service_line.service_line_id'], name=op.f('fk_remittance_adjustment_service_line_id_remittance_service_line'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('adjustment_id', name=op.f('pk_remittance_adjustment'))
    )
    op.create_index('ix_remittance_adjustment_claim', 'remittance_adjustment', ['claim_payment_id'], unique=False)
    op.create_index('ix_remittance_adjustment_client_group_reason', 'remittance_adjustment', ['client_id', 'cas_group_code', 'reason_code'], unique=False)
    op.create_index('ix_remittance_adjustment_line', 'remittance_adjustment', ['service_line_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_remittance_adjustment_line', table_name='remittance_adjustment')
    op.drop_index('ix_remittance_adjustment_client_group_reason', table_name='remittance_adjustment')
    op.drop_index('ix_remittance_adjustment_claim', table_name='remittance_adjustment')
    op.drop_table('remittance_adjustment')
    op.drop_index('ix_remittance_service_line_client_procedure', table_name='remittance_service_line')
    op.drop_table('remittance_service_line')
    op.drop_index('ix_provider_adjustment_remittance', table_name='provider_adjustment')
    op.drop_index('ix_provider_adjustment_reference', table_name='provider_adjustment')
    op.drop_table('provider_adjustment')
    op.drop_index('ix_claim_payment_remittance', table_name='claim_payment')
    op.drop_index('ix_claim_payment_payer_control', table_name='claim_payment')
    op.drop_index('ix_claim_payment_client_claim', table_name='claim_payment')
    op.drop_table('claim_payment')
    op.drop_index('ix_remittance_payment_date', table_name='remittance')
    op.drop_index('ix_remittance_client_payer', table_name='remittance')
    op.drop_table('remittance')
    op.drop_index('ix_payer_alias_payer_id', table_name='payer_alias')
    op.drop_table('payer_alias')
    op.drop_index('ix_raw_file_status', table_name='raw_file')
    op.drop_index('ix_raw_file_client_received', table_name='raw_file')
    op.drop_table('raw_file')
    op.drop_table('payer')
