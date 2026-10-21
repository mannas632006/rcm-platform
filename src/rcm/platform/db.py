"""Database engine, session management and the declarative base.

Two connections, always, and they are not interchangeable:

- `engine()` -- the **application** role (`rcm_app`). It holds no UPDATE or DELETE
  on the audit tables. Everything the API and the workers do goes through it.
- `migration_engine()` -- the **owner** role (`rcm_migrator`), used only by Alembic.

DR-06 says the audit tables are append-only, and CLAUDE.md M0.4 says the *test*
must prove the application role cannot modify them. That proof only means
something if the application genuinely runs as a role without the grant. Running
the app as the owner would leave the guarantee resting on nobody writing the
wrong query, which is not a guarantee.

SR-11: every query is parameterised. SQLAlchemy Core and the ORM do this; string
interpolation into SQL fails review.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from rcm.platform.config import Settings, get_settings

#: Deterministic constraint names. Without this, Alembic autogenerate produces
#: unnamed constraints that later migrations cannot drop by name.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the platform."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _build_engine(url: str, settings: Settings, **kwargs: Any) -> Engine:
    return create_engine(
        url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        # Money and identifiers are exact types; `future` semantics are the 2.x
        # default but stated here because a 1.x-style engine would silently
        # change transaction behaviour.
        future=True,
        **kwargs,
    )


@lru_cache(maxsize=1)
def engine() -> Engine:
    """The application engine, running as the restricted role."""
    settings = get_settings()
    return _build_engine(
        settings.database_url.get_secret_value(),
        settings,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


@lru_cache(maxsize=1)
def migration_engine() -> Engine:
    """The owner engine. Alembic only -- never serve a request through this."""
    settings = get_settings()
    return _build_engine(
        settings.migration_database_url.get_secret_value(),
        settings,
        poolclass=None,
    )


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine(),
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional scope.

    Commits on success, rolls back on any exception. PD-2 depends on this being
    the only way work reaches the database: an audit row written in the same
    transaction as the change it records must disappear when that change is
    rolled back, and it can only do so if both share this scope.
    """
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped transactional session."""
    with session_scope() as session:
        yield session


def reset_engines() -> None:
    """Dispose the cached engines and drop them from the cache.

    Disposal has to happen before `cache_clear()`, not instead of it. Clearing
    the cache alone drops the only reference to an `Engine` whose pool still
    holds open psycopg connections; nothing then closes them, so the garbage
    collector does it at interpreter shutdown and psycopg emits

        ResourceWarning: <psycopg.Connection ...> was deleted while still open

    which `filterwarnings = ["error"]` turns into a failed test session after
    every test has already passed. That is how this surfaced: the integration
    suite reported "2 passed" and exited 1.

    The leak is not confined to tests. Any process that calls this -- a worker
    rebuilding its engine after a configuration change, say -- would abandon a
    live connection pool each time.

    `cache_info().currsize` guards the call: invoking a cached factory to
    dispose it would otherwise *create* the very engine being discarded.
    """
    for engine_factory in (engine, migration_engine):
        if engine_factory.cache_info().currsize:
            engine_factory().dispose()
    for cached in (engine, migration_engine, session_factory):
        cached.cache_clear()
