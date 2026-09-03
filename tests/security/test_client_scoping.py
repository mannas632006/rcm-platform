"""PD-20 / SR-21: client scoping is enforced at the query layer.

The directive's own test is *"handler without filter still cannot leak"*, so that
is the central case here. A suite that only checked correctly-written queries
would prove the filter works when it is asked for -- which is not the property
being claimed.

Runs on SQLite: `with_loader_criteria` is ORM-level, so the behaviour is identical
on Postgres. The Postgres integration suite additionally proves row-level security
as a second, independent layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import String, create_engine, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from rcm.platform.db import Base
from rcm.platform.errors import ClientScopeViolationError
from rcm.platform.rbac import ClientScope, Principal, Role
from rcm.platform.scoping import (
    ClientScoped,
    UnscopedQueryError,
    principal_scope,
    system_scope,
)

pytestmark = pytest.mark.security

PRACTICE_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
PRACTICE_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


class ScopedThing(Base, ClientScoped):
    """A stand-in for any client-scoped domain table."""

    __tablename__ = "scoped_thing_test_fixture"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(32))


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    ScopedThing.__table__.create(engine)
    with Session(engine) as db:
        # Seeded with system scope: this is setup, and it is saying so.
        with system_scope(reason="test fixture seeding"):
            db.add_all(
                [
                    ScopedThing(practice_id=PRACTICE_A, label="a-one"),
                    ScopedThing(practice_id=PRACTICE_A, label="a-two"),
                    ScopedThing(practice_id=PRACTICE_B, label="b-one"),
                ]
            )
            db.commit()
        yield db


def ar_specialist(*practices: uuid.UUID) -> Principal:
    return Principal(
        subject="user:ar-1",
        roles=frozenset({Role.AR_SPECIALIST}),
        practice_ids=frozenset(practices),
    )


def ops_manager() -> Principal:
    return Principal(subject="user:ops-1", roles=frozenset({Role.OPS_MANAGER}))


# ---------------------------------------------------------------------------
# The named invariant test (CLAUDE.md section 7.2)
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_cross_client_access_denied(session: Session) -> None:
    """A principal scoped to practice A cannot read practice B's rows."""
    with principal_scope(ar_specialist(PRACTICE_A)):
        labels = set(session.scalars(select(ScopedThing.label)).all())
        assert labels == {"a-one", "a-two"}

        # Naming B's id explicitly does not help.
        targeted = session.scalars(
            select(ScopedThing).where(ScopedThing.practice_id == PRACTICE_B)
        ).all()
        assert targeted == []


# ---------------------------------------------------------------------------
# The property PD-20 actually claims
# ---------------------------------------------------------------------------


def test_handler_without_filter_cannot_leak(session: Session) -> None:
    """The control works when the caller does nothing to invoke it.

    This is the difference between scoping at the query layer and scoping in
    handlers. The statement below is what a handler written by someone who has
    never heard of client scoping looks like.
    """
    with principal_scope(ar_specialist(PRACTICE_A)):
        everything = session.scalars(select(ScopedThing)).all()

    assert {row.label for row in everything} == {"a-one", "a-two"}
    assert all(row.practice_id == PRACTICE_A for row in everything)


def test_query_without_a_principal_is_refused(session: Session) -> None:
    """PD-18 applied to authorisation: unknown scope denies, it does not permit.

    The tempting alternative -- treat "no principal" as "no filter" -- turns
    every forgotten binding into a silent full-table read.
    """
    with pytest.raises(UnscopedQueryError):
        session.scalars(select(ScopedThing)).all()


def test_assigned_principal_with_no_practices_sees_nothing(session: Session) -> None:
    """An empty assignment list means no rows, not all rows.

    `IN ()` is the kind of edge that inverts a filter if it is built by string
    concatenation, so it is asserted rather than assumed.
    """
    with principal_scope(ar_specialist()), pytest.raises(ClientScopeViolationError):
        session.scalars(select(ScopedThing)).all()


def test_all_scope_role_sees_every_client(session: Session) -> None:
    """SRS section 11.5: Ops Manager's client scope is All."""
    with principal_scope(ops_manager()) as principal:
        assert principal.client_scope is ClientScope.ALL
        labels = set(session.scalars(select(ScopedThing.label)).all())
    assert labels == {"a-one", "a-two", "b-one"}


def test_system_scope_is_explicit_and_greppable(session: Session) -> None:
    """Cross-client work is possible, but only by saying so."""
    with system_scope(reason="nightly mart rebuild"):
        labels = set(session.scalars(select(ScopedThing.label)).all())
    assert labels == {"a-one", "a-two", "b-one"}


def test_system_scope_requires_a_reason() -> None:
    """An unexplained bypass is the thing a reviewer needs to see."""
    with pytest.raises(ValueError, match="reason"), system_scope(reason="   "):
        pass  # pragma: no cover - the context manager raises on entry


def test_multiple_practices_are_all_visible(session: Session) -> None:
    with principal_scope(ar_specialist(PRACTICE_A, PRACTICE_B)):
        labels = set(session.scalars(select(ScopedThing.label)).all())
    assert labels == {"a-one", "a-two", "b-one"}
