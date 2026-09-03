"""Client scoping enforced at the query layer (PD-20, SR-21, S-5).

PD-20 is specific about where this lives: *"Client scoping enforced at the query
layer, not the handler"*, and its stated test is *"handler without filter still
cannot leak"*. That rules out the obvious implementation. A `WHERE practice_id =`
that each handler remembers to add is not this control, because the failure mode
being defended against is a handler that forgets.

So the filter is attached to the ORM's execution path. Any `SELECT` touching a
`ClientScoped` entity gets the predicate injected by SQLAlchemy itself, through
`with_loader_criteria`, including through relationship loads and joins the query's
author never named.

**Absence of a principal denies rather than permits.** A query with no principal
bound raises `UnscopedQueryError` instead of returning every tenant's rows. This
is PD-18 applied to authorisation: the uncertain case fails closed. It means a
background job that legitimately spans clients must say so out loud, via
`system_scope()`, which is greppable and reviewable -- unlike an empty filter,
which looks like every other query.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Mapped, Session, mapped_column, with_loader_criteria
from sqlalchemy.orm.session import ORMExecuteState

from rcm.platform.errors import ClientScopeViolationError
from rcm.platform.rbac import ClientScope, Principal

_current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)
#: Set only inside `system_scope()`. Separate from the principal so that "no
#: principal" and "deliberately unscoped" can never be confused for each other.
_system_scope: ContextVar[bool] = ContextVar("system_scope", default=False)


class UnscopedQueryError(RuntimeError):
    """A client-scoped entity was queried with no principal and no system scope.

    Not a `PlatformError`: this is a programming fault, not a request outcome. It
    should never reach an API client, and if it does the catch-all handler turns
    it into a generic 500 rather than leaking that scoping exists.
    """


class ClientScoped:
    """Mixin for every table holding one client's data.

    Inheriting this is what subjects a table to the filter. A new table that holds
    practice data and does not inherit it is invisible to this control -- which is
    why `tests/security/test_client_scoping.py` asserts the expected set of scoped
    tables, so adding one without the mixin fails the build rather than leaking
    quietly.
    """

    practice_id: Mapped[UUID] = mapped_column(nullable=False, index=True)


def current_principal() -> Principal | None:
    return _current_principal.get()


@contextmanager
def principal_scope(principal: Principal) -> Iterator[Principal]:
    """Bind a principal for the duration of a request or task."""
    token: Token[Principal | None] = _current_principal.set(principal)
    try:
        yield principal
    finally:
        _current_principal.reset(token)


@contextmanager
def system_scope(*, reason: str) -> Iterator[None]:
    """Run queries across every client, deliberately.

    For ingestion, nightly sweeps and mart builds -- work that is genuinely
    cross-tenant. `reason` is required so the call site explains itself; it is
    recorded by the caller's audit event, not swallowed here.

    This is the only way to bypass the filter, and it is one grep away from any
    reviewer asking "what runs unscoped?".
    """
    if not reason.strip():
        raise ValueError("system_scope requires a reason")
    token = _system_scope.set(True)
    try:
        yield
    finally:
        _system_scope.reset(token)


@event.listens_for(Session, "do_orm_execute")
def _apply_client_scope(state: ORMExecuteState) -> None:
    """Inject the practice filter into every ORM SELECT.

    Runs for the ORM's own relationship loads as well as top-level queries, so a
    scoped row cannot be reached indirectly through an unscoped parent.
    """
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        # Column and relationship loads refresh rows already fetched under a
        # scope that was checked when they were first loaded. Re-filtering them
        # would break lazy loading without adding a guarantee.
        return

    if not _touches_scoped_entity(state):
        return

    if _system_scope.get():
        return

    principal = _current_principal.get()
    if principal is None:
        raise UnscopedQueryError(
            "a client-scoped entity was queried with no principal bound. "
            "Use principal_scope(...) for user work, or system_scope(reason=...) "
            "for deliberately cross-client work."
        )

    if principal.client_scope is ClientScope.ALL:
        return

    if not principal.practice_ids:
        # An assigned-only principal with no assignments sees nothing. Returning
        # everything here would be the exact inversion of the intended rule.
        raise ClientScopeViolationError(subject=principal.subject, reason="no_practices_assigned")

    allowed = tuple(principal.practice_ids)
    state.statement = state.statement.options(
        with_loader_criteria(
            ClientScoped,
            lambda cls: cls.practice_id.in_(allowed),
            include_aliases=True,
        )
    )


def _touches_scoped_entity(state: ORMExecuteState) -> bool:
    """Whether this statement reads any `ClientScoped` mapper."""
    for descriptor in state.all_mappers:
        mapper_class: Any = descriptor.class_
        if isinstance(mapper_class, type) and issubclass(mapper_class, ClientScoped):
            return True
    return False
