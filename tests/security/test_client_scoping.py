"""Client scoping is enforced at the query layer, not the handler (PD-20, S-5).

CLAUDE.md M0.7 states the acceptance condition precisely:

    "Test: user scoped to client A cannot read client B by any route"

and PD-20 says where the control must live:

    "Client scoping enforced at the query layer, not the handler."

The second is the harder claim and the one these tests are built around. A
handler-level check is only as good as every future handler author's memory; the
request still arrives at the database unfiltered. So the tests below do not call
any handler. They call the query layer directly -- the way a careless repository
function would -- and assert that the unscoped path is impossible rather than
merely discouraged.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import String, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.auth import (
    Permission,
    Principal,
    Role,
    UnscopedQueryError,
    assert_within_scope,
    scoped_query,
    system_principal,
)
from rcm.platform.db import Base
from rcm.platform.errors import AuthorizationDeniedError, ClientScopeViolationError

pytestmark = pytest.mark.security


class ScopedThing(Base):
    """A client-scoped entity, standing in for claim, denial, work item.

    Defined here rather than imported so these tests keep working as the real
    entities arrive in M1 and M3 -- the control is what is under test, not any
    particular table.
    """

    __tablename__ = "test_scoped_thing"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)


class GlobalThing(Base):
    """An entity with no client dimension, such as the CARC code list."""

    __tablename__ = "test_global_thing"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    label: Mapped[str] = mapped_column(String(64), nullable=False)


CLIENT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
CLIENT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")


def principal_for(*clients: uuid.UUID, role: Role = Role.AR_SPECIALIST) -> Principal:
    return Principal(
        subject=f"user-{role.value.lower()}",
        roles=frozenset({role}),
        client_ids=frozenset(clients),
    )


def _rendered(statement: object) -> str:
    """Compile against the real dialect.

    Deliberately PostgreSQL rather than the default: the default dialect renders
    a UUID without hyphens, so an assertion written against it would pass while
    testing a string that never reaches the database.
    """
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@pytest.mark.invariant
def test_cross_client_access_denied() -> None:
    """A principal scoped to A cannot express a query that returns B's rows.

    The named invariant from CLAUDE.md section 7.2. Asserted on the compiled SQL
    rather than on returned rows, because the point is that the restriction is
    in the statement itself -- it holds regardless of what the database contains
    or which handler built it.
    """
    principal = principal_for(CLIENT_A)
    statement = scoped_query(select(ScopedThing), ScopedThing, principal)
    sql = _rendered(statement)

    assert str(CLIENT_A) in sql
    assert str(CLIENT_B) not in sql
    assert "client_id IN" in sql.replace("\n", " ")


def test_scope_filter_survives_additional_where_clauses() -> None:
    """Narrowing a scoped statement cannot widen it.

    The realistic failure is not someone deleting the scope call; it is someone
    adding `.where(...)` afterwards and assuming it replaced the filter.
    """
    principal = principal_for(CLIENT_A)
    statement = scoped_query(select(ScopedThing), ScopedThing, principal).where(
        ScopedThing.label == "anything"
    )
    sql = _rendered(statement)

    assert str(CLIENT_A) in sql
    assert str(CLIENT_B) not in sql


def test_principal_cannot_widen_its_own_scope() -> None:
    """`Principal` is frozen, so a handler cannot add a client id to it."""
    principal = principal_for(CLIENT_A)
    with pytest.raises((AttributeError, TypeError)):
        principal.client_ids = frozenset({CLIENT_A, CLIENT_B})  # type: ignore[misc]


def test_empty_scope_matches_nothing_rather_than_everything() -> None:
    """Fail closed (PD-18).

    A naive `WHERE client_id IN ()` is a SQL error or, worse, optimised away. The
    rendered statement must still constrain the query.
    """
    principal = principal_for()
    statement = scoped_query(select(ScopedThing), ScopedThing, principal)
    sql = _rendered(statement).replace("\n", " ")

    assert "WHERE" in sql
    assert "client_id IN" in sql


def test_unscoped_entity_cannot_be_scoped_silently() -> None:
    """An entity with no `client_id` raises rather than returning an open query.

    Returning the statement untouched would be the dangerous behaviour: the
    caller believes it is scoped and it is not.
    """
    principal = principal_for(CLIENT_A)
    with pytest.raises(UnscopedQueryError, match="cannot be client-scoped"):
        scoped_query(select(GlobalThing), GlobalThing, principal)


def test_system_principal_must_name_its_clients() -> None:
    """An unattended task may not query across every tenant at once."""
    unscoped_system = Principal(
        subject="system:nightly",
        roles=frozenset({Role.SYSTEM}),
        client_ids=frozenset(),
        is_system=True,
    )
    with pytest.raises(UnscopedQueryError, match="iterate tenants"):
        scoped_query(select(ScopedThing), ScopedThing, unscoped_system)

    scoped_system = system_principal(task="nightly", client_ids=[CLIENT_A])
    assert str(CLIENT_A) in _rendered(scoped_query(select(ScopedThing), ScopedThing, scoped_system))


def test_rows_from_outside_the_scope_are_rejected_after_the_fact() -> None:
    """Defence in depth: a join that reintroduces a foreign row is caught."""
    principal = principal_for(CLIENT_A)
    foreign = ScopedThing(id=uuid.uuid4(), client_id=CLIENT_B, label="other tenant")

    with pytest.raises(ClientScopeViolationError):
        assert_within_scope([foreign], principal)


def test_require_client_rejects_a_foreign_identifier() -> None:
    principal = principal_for(CLIENT_A)
    principal.require_client(CLIENT_A)
    with pytest.raises(ClientScopeViolationError):
        principal.require_client(CLIENT_B)


def test_scope_violation_is_indistinguishable_from_any_other_denial() -> None:
    """403 with the same body, so a caller cannot probe which ids exist.

    A 404 for "not yours" and a 403 for "exists but forbidden" together form an
    oracle that enumerates another tenant's identifiers.
    """
    scope_error = ClientScopeViolationError(requested_client=str(CLIENT_B))
    plain_error = AuthorizationDeniedError(permission=str(Permission.CLAIM_READ))

    assert scope_error.status == plain_error.status == 403
    assert scope_error.title == plain_error.title
    assert scope_error.detail == plain_error.detail
