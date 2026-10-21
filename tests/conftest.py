"""Shared fixtures.

Database fixtures are opt-in: unit tests never request them, so `pytest tests/unit`
runs without Docker. Anything that does request them needs `make up` and
`make migrate` first.

When Postgres is unreachable these fixtures **fail**, they do not skip. CLAUDE.md
section 7.5: a skipped test is a failing test. A suite that quietly skips the
append-only proof whenever the database is down would report green on exactly the
machines where nobody checked.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from rcm.platform.config import get_settings


def _engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True)


def _require_reachable(engine: Engine, role: str) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.fail(
            f"Postgres is not reachable as {role}. Run `make up` then `make migrate`.\n"
            f"Driver said: {type(exc.orig).__name__}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def app_engine() -> Iterator[Engine]:
    """Connects as `rcm_app` -- the role the application actually runs as."""
    engine = _engine(get_settings().database_url.get_secret_value())
    _require_reachable(engine, "rcm_app")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    """Connects as `rcm_migrator`, the schema owner.

    Used only for test setup and cleanup that the application role is, by
    design, not allowed to do -- such as removing committed audit rows.
    """
    engine = _engine(get_settings().migration_database_url.get_secret_value())
    _require_reachable(engine, "rcm_migrator")
    yield engine
    engine.dispose()
