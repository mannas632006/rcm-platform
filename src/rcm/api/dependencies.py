"""FastAPI dependencies: authentication, authorisation and the request session.

The dependencies here are thin. They turn a request into a `Principal` and assert
a permission; they do not decide which rows that principal may see. That is the
query layer's job, and keeping the two apart is what PD-20 is about -- a
dependency that both authenticated *and* filtered would make the filter
skippable by writing a route that omits it.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterator
from typing import Annotated, Any, Final

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.auth import Permission, Principal, principal_from_token
from rcm.platform.db import get_session, session_scope
from rcm.platform.errors import AuthenticationFailedError

#: `auto_error=False` so a missing header raises our own error type rather than
#: Starlette's bare 403, which would bypass the RFC 7807 handler.
_bearer: Final = HTTPBearer(auto_error=False, scheme_name="OIDC bearer token")


def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Authenticate the caller (S-3).

    The principal is also stashed on `request.state` because the idempotency
    middleware namespaces its keys per principal and runs outside the dependency
    graph.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationFailedError()

    # The idempotency middleware verifies the same token before routing, to
    # namespace its keys per principal. Reuse that result rather than verifying
    # twice -- on the JWKS path a second verification is a second cache lookup
    # and, on a cold key, a second network round trip.
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached

    principal = principal_from_token(credentials.credentials)
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
DbSession = Annotated[Session, Depends(get_session)]


def require_permission(permission: Permission) -> Any:
    """Dependency factory asserting a single permission (S-4).

    Returns the `Depends` itself, so it is used as
    `dependencies=[require_permission(Permission.CLAIM_WRITE)]` on the route
    decorator. The check is declared on the route, so the route's authority
    is readable from its signature rather than from the body of its handler.
    """

    def _check(principal: CurrentPrincipal) -> Principal:
        principal.require(permission)
        return principal

    return Depends(_check)


def audited_session(
    request: Request,
    principal: CurrentPrincipal,
) -> Iterator[Session]:
    """A session that records the PHI access this request performs (SR-01, PD-2).

    The audit row is written inside the request's transaction, so a request that
    fails and rolls back leaves no claim of access that did not happen -- which
    is what PD-2 means by "in the same transaction as the change".

    It is flushed before the route runs. The commit happens in teardown, after
    the response is sent; an audit insert that failed only then would have
    served PHI with no record of it. Flushed first, an access that cannot be
    audited is refused before anything is read (PD-18).
    """
    with session_scope() as session:
        record_audit_event(
            session,
            actor_type=ActorType.SYSTEM if principal.is_system else ActorType.USER,
            actor_id=principal.subject,
            action=f"{request.method} {request.url.path}",
            entity_type="http_request",
            entity_id=None,
            ip_address=_ip(request),
            session_id=principal.session_id,
        )
        session.flush()
        yield session


def _ip(request: Request) -> str | None:
    """The client's address, or None when the peer is not one (a test client,
    a unix socket): recorded as unknown, never as something it is not."""
    host = request.client.host if request.client else None
    if host is None:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


AuditedSession = Annotated[Session, Depends(audited_session)]


def client_id_in_scope(principal: CurrentPrincipal, client_id: uuid.UUID) -> uuid.UUID:
    """Validate a client id supplied as a path or query parameter (PD-20).

    Note this is a convenience for routes that take an explicit client id, not
    the scoping control. Rows are filtered by `rcm.platform.auth.scoped_query`
    whether or not a route uses this.
    """
    principal.require_client(client_id)
    return client_id
