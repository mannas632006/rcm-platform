"""Alembic environment.

Runs as the **owner** role (`rcm_migrator`), never the application role. The
application role deliberately lacks the privileges migrations need -- that is what
makes DR-06's append-only guarantee real rather than advisory.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

# Importing a model module is what registers its tables on Base.metadata.
# Autogenerate compares the database against whatever is registered here, so a
# model not imported is a model Alembic will cheerfully propose dropping.
from rcm.domain.claim import Claim, ClaimLine  # noqa: F401 - registers metadata
from rcm.domain.payer import Payer, PayerAlias  # noqa: F401 - registers metadata
from rcm.domain.remittance import (  # noqa: F401 - registers metadata
    ClaimPayment,
    ProviderAdjustment,
    RawFile,
    Remittance,
    RemittanceAdjustment,
    RemittanceServiceLine,
)
from rcm.platform.audit import AuditEvent, DecisionRecord  # noqa: F401 - registers metadata
from rcm.platform.db import Base, migration_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a connection, for review before applying to staging."""
    context.configure(
        url=str(migration_engine().url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = migration_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Type changes are a common source of silent data loss on money
            # columns; make autogenerate notice them.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
