"""Authentication, roles and client scoping (M0.7; S-3, S-4, S-5, PD-20).

Three separate things live here, and conflating them is how tenant leaks happen:

**Authentication** establishes *who is calling*. A bearer token is verified
against the OIDC issuer's keys, and its claims become a `Principal`. Outside
local and CI the only accepted signature algorithms are asymmetric -- a
symmetric secret shared with every service means any service can mint a token
for any user.

**Authorisation** decides *what that principal may do*. Roles map to permissions
through a fixed matrix (SRS section 11.5). A permission check answers one
question, in one place, and the answer does not depend on which route asked.

**Client scoping** decides *which rows exist at all* for this principal. This is
the one that matters most and is the one PD-20 is about:

    "Client scoping enforced at the query layer, not the handler."

The distinction is not pedantry. A handler-level check is a filter someone can
forget to write; the request still reaches the database unfiltered, and the only
thing standing between one client's data and another's is that every author of
every future handler remembered. `scoped_query()` inverts that: the filter is
applied when the statement is built, and a handler that forgets to scope gets a
statement that cannot return foreign rows rather than one that silently does.

`tests/security/test_client_scoping.py` proves it by calling the query layer
directly with no handler in the picture at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import jwt
from jwt import PyJWKClient
from sqlalchemy import Select
from sqlalchemy.orm import InstrumentedAttribute

from rcm.platform.config import Settings, get_settings
from rcm.platform.errors import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    ClientScopeViolationError,
)
from rcm.platform.logging import get_logger

log = get_logger(__name__)

#: The column every client-scoped table carries. Named once so a typo cannot
#: silently produce an unscoped query.
CLIENT_SCOPE_COLUMN: Final = "client_id"


class Role(StrEnum):
    """Operator roles (SRS section 11.5).

    Deliberately coarse. A role per job title produces a matrix nobody maintains;
    these are the five distinct *authority levels* the work actually has.
    """

    AR_SPECIALIST = "AR_SPECIALIST"
    DENIAL_SPECIALIST = "DENIAL_SPECIALIST"
    CODER = "CODER"
    OPERATIONS_MANAGER = "OPERATIONS_MANAGER"
    RULES_AUTHOR = "RULES_AUTHOR"
    ADMINISTRATOR = "ADMINISTRATOR"
    #: Celery tasks and schedulers. Never issued to a human.
    SYSTEM = "SYSTEM"


class Permission(StrEnum):
    """What a principal may do. Checked, never inferred from the role name."""

    CLAIM_READ = "claim:read"
    CLAIM_WRITE = "claim:write"
    CLAIM_SUBMIT = "claim:submit"
    DENIAL_READ = "denial:read"
    DENIAL_DISPOSITION = "denial:disposition"
    APPEAL_SIGN = "appeal:sign"
    CODING_READ = "coding:read"
    CODING_WRITE = "coding:write"
    RULE_READ = "rule:read"
    RULE_AUTHOR = "rule:author"
    RULE_PROMOTE = "rule:promote"
    PAYER_REGISTRY_WRITE = "payer_registry:write"
    REPORT_READ = "report:read"
    AUDIT_READ = "audit:read"
    USER_ADMIN = "user:admin"


#: Role -> permissions. The single source of truth for S-4.
#:
#: Two properties worth stating because they are decisions, not oversights:
#: APPEAL_SIGN is held only by the denial specialist and the operations manager,
#: because M5.7 requires a human signature on an appeal and a signature from a
#: role that cannot read denials would be meaningless. RULE_PROMOTE is separate
#: from RULE_AUTHOR so that writing a rule and switching it on for every client
#: are not the same act (M4.5).
ROLE_PERMISSIONS: Final[dict[Role, frozenset[Permission]]] = {
    Role.AR_SPECIALIST: frozenset(
        {
            Permission.CLAIM_READ,
            Permission.CLAIM_WRITE,
            Permission.DENIAL_READ,
            Permission.REPORT_READ,
        }
    ),
    Role.DENIAL_SPECIALIST: frozenset(
        {
            Permission.CLAIM_READ,
            Permission.CLAIM_WRITE,
            Permission.DENIAL_READ,
            Permission.DENIAL_DISPOSITION,
            Permission.APPEAL_SIGN,
            Permission.REPORT_READ,
        }
    ),
    Role.CODER: frozenset(
        {
            Permission.CLAIM_READ,
            Permission.CODING_READ,
            Permission.CODING_WRITE,
            Permission.REPORT_READ,
        }
    ),
    Role.OPERATIONS_MANAGER: frozenset(
        {
            Permission.CLAIM_READ,
            Permission.CLAIM_WRITE,
            Permission.CLAIM_SUBMIT,
            Permission.DENIAL_READ,
            Permission.DENIAL_DISPOSITION,
            Permission.APPEAL_SIGN,
            Permission.CODING_READ,
            Permission.RULE_READ,
            Permission.REPORT_READ,
            Permission.AUDIT_READ,
        }
    ),
    Role.RULES_AUTHOR: frozenset(
        {
            Permission.CLAIM_READ,
            Permission.DENIAL_READ,
            Permission.RULE_READ,
            Permission.RULE_AUTHOR,
            Permission.RULE_PROMOTE,
            Permission.PAYER_REGISTRY_WRITE,
            Permission.REPORT_READ,
        }
    ),
    Role.ADMINISTRATOR: frozenset(
        {
            Permission.USER_ADMIN,
            Permission.AUDIT_READ,
            Permission.REPORT_READ,
        }
    ),
    Role.SYSTEM: frozenset(
        {
            Permission.CLAIM_READ,
            Permission.CLAIM_WRITE,
            Permission.CLAIM_SUBMIT,
            Permission.DENIAL_READ,
            Permission.CODING_READ,
            Permission.RULE_READ,
        }
    ),
}

#: The administrator deliberately holds no CLAIM_READ. Administering users is not
#: a clinical or financial role, and HIPAA minimum-necessary says the person who
#: resets passwords has no business reading claims. Breaking this needs an ADR.
assert Permission.CLAIM_READ not in ROLE_PERMISSIONS[Role.ADMINISTRATOR]


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    `client_ids` is the set of tenants this principal may see, and it is frozen
    at authentication time from the token. Nothing downstream may widen it --
    there is no setter, and the dataclass is frozen, so a handler cannot "just
    add" a client id to serve a convenient query.
    """

    subject: str
    roles: frozenset[Role]
    client_ids: frozenset[uuid.UUID]
    session_id: uuid.UUID | None = None
    #: True for Celery workers and schedulers (`Role.SYSTEM`).
    is_system: bool = False
    claims: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def permissions(self) -> frozenset[Permission]:
        granted: set[Permission] = set()
        for role in self.roles:
            granted |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(granted)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Raise unless the principal holds `permission`.

        The denial carries the permission, not the principal's full claim set:
        an error payload is one of the four PHI leak paths (PD-13), and a role
        list is enough to fingerprint an individual in a small team.
        """
        if not self.has(permission):
            log.warning("authz.denied", permission=str(permission), subject_hash=hash(self.subject))
            raise AuthorizationDeniedError(permission=str(permission))

    def require_client(self, client_id: uuid.UUID) -> None:
        """Raise unless `client_id` is inside this principal's scope (PD-20)."""
        if client_id not in self.client_ids:
            raise ClientScopeViolationError(requested_client=str(client_id))


#: A principal for unattended work. Carries `Role.SYSTEM` and an explicit client
#: scope -- a worker still runs on behalf of one tenant's data, and a task that
#: needs every tenant iterates them rather than holding a wildcard.
def system_principal(*, task: str, client_ids: Iterable[uuid.UUID]) -> Principal:
    return Principal(
        subject=f"system:{task}",
        roles=frozenset({Role.SYSTEM}),
        client_ids=frozenset(client_ids),
        is_system=True,
    )


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------

_JWKS_CLIENT: PyJWKClient | None = None


def _jwks_client(settings: Settings) -> PyJWKClient:
    global _JWKS_CLIENT
    if _JWKS_CLIENT is None:
        _JWKS_CLIENT = PyJWKClient(
            f"{settings.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs",
            cache_keys=True,
        )
    return _JWKS_CLIENT


def reset_jwks_client() -> None:
    """Drop the cached JWKS client. Tests use this after changing settings."""
    global _JWKS_CLIENT
    _JWKS_CLIENT = None


def _decode(token: str, settings: Settings) -> dict[str, Any]:
    """Verify a bearer token and return its claims.

    The dev path exists so the stack runs without a Keycloak container. It is
    reachable only in local and CI, and `Settings.validate_for_environment`
    refuses to start a deployed environment that has `dev_jwt_secret` set -- so
    the stub cannot be switched on in production by setting one variable.
    """
    if settings.dev_jwt_secret is not None and settings.environment in ("local", "ci"):
        return dict(
            jwt.decode(
                token,
                settings.dev_jwt_secret.get_secret_value(),
                algorithms=["HS256"],
                audience=settings.oidc_audience,
                issuer=settings.oidc_issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        )

    signing_key = _jwks_client(settings).get_signing_key_from_jwt(token)
    return dict(
        jwt.decode(
            token,
            signing_key.key,
            algorithms=list(settings.jwt_algorithms),
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    )


def principal_from_token(token: str, settings: Settings | None = None) -> Principal:
    """Verify a bearer token and build the `Principal` it describes.

    Every failure here is the same `AuthenticationFailedError` with no detail
    about which check failed. Telling a caller whether the signature, the
    audience or the expiry was wrong is a probing oracle.
    """
    settings = settings or get_settings()
    try:
        claims = _decode(token, settings)
    except jwt.PyJWTError as exc:
        log.warning("auth.token_rejected", reason=type(exc).__name__)
        raise AuthenticationFailedError() from exc

    raw_roles = claims.get("roles") or claims.get("realm_access", {}).get("roles") or []
    roles = frozenset(Role(r) for r in raw_roles if r in Role.__members__.values())
    if not roles:
        log.warning("auth.no_recognised_roles")
        raise AuthenticationFailedError()

    try:
        client_ids = frozenset(uuid.UUID(str(c)) for c in claims.get("client_ids", []))
    except ValueError as exc:
        raise AuthenticationFailedError() from exc

    # PD-18, fail closed: a human principal with no client scope sees nothing.
    # An empty scope is not an error at authentication -- it is a legitimate
    # state for a newly provisioned account -- but every scoped query it makes
    # returns nothing, rather than everything.
    session_raw = claims.get("sid")
    return Principal(
        subject=str(claims["sub"]),
        roles=roles,
        client_ids=client_ids,
        session_id=uuid.UUID(session_raw) if session_raw else None,
        is_system=Role.SYSTEM in roles,
        claims=claims,
    )


def issue_dev_token(
    *,
    subject: str,
    roles: Sequence[Role],
    client_ids: Sequence[uuid.UUID],
    settings: Settings | None = None,
    expires_in: timedelta = timedelta(hours=1),
    session_id: uuid.UUID | None = None,
) -> str:
    """Mint a token for local development and tests.

    Refuses outside local and CI even if a secret is somehow present, so this
    function cannot become a backdoor by configuration alone.
    """
    settings = settings or get_settings()
    if settings.environment not in ("local", "ci"):
        raise RuntimeError(f"issue_dev_token is not available in {settings.environment!r}")
    if settings.dev_jwt_secret is None:
        raise RuntimeError("dev_jwt_secret is not configured")

    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iss": settings.oidc_issuer,
        "aud": settings.oidc_audience,
        "iat": now,
        "exp": now + expires_in,
        "roles": [str(r) for r in roles],
        "client_ids": [str(c) for c in client_ids],
    }
    if session_id is not None:
        payload["sid"] = str(session_id)
    return jwt.encode(payload, settings.dev_jwt_secret.get_secret_value(), algorithm="HS256")


# ---------------------------------------------------------------------------
# Client scoping at the query layer (PD-20, S-5)
# ---------------------------------------------------------------------------


class UnscopedQueryError(RuntimeError):
    """A client-scoped entity was queried without a scope.

    Raised at the query layer, not the handler, so that forgetting to scope is a
    loud failure rather than a quiet data leak.
    """


def scoped_query[S](
    statement: Select[tuple[S]],
    entity: type[Any],
    principal: Principal,
) -> Select[tuple[S]]:
    """Apply the principal's client scope to `statement`.

    This is the control PD-20 names. Every read of a client-scoped table goes
    through here; `tests/security/test_client_scoping.py` asserts that a
    statement built without it cannot reach a repository function at all.

    An empty scope yields a statement matching nothing. That is the fail-closed
    reading: a principal with no clients sees no rows, rather than the
    unconstrained query that `WHERE client_id IN ()` would otherwise become.
    """
    column = getattr(entity, CLIENT_SCOPE_COLUMN, None)
    if column is None or not isinstance(column, InstrumentedAttribute):
        raise UnscopedQueryError(
            f"{entity.__name__} has no {CLIENT_SCOPE_COLUMN}; it cannot be client-scoped. "
            "If the table is genuinely global, query it through an explicitly named "
            "unscoped repository function so the exception is visible in review."
        )

    if principal.is_system and not principal.client_ids:
        raise UnscopedQueryError(
            "a system principal must name the clients its task runs for; "
            "iterate tenants rather than querying across all of them"
        )

    return statement.where(column.in_(principal.client_ids))


def assert_within_scope(rows: Iterable[Any], principal: Principal) -> None:
    """Defence in depth: verify returned rows are inside the scope.

    `scoped_query` is the control. This catches the case where a hand-written
    join reintroduced a row from outside the scope through a related table, which
    a `WHERE` on the primary entity would not prevent.
    """
    for row in rows:
        client_id = getattr(row, CLIENT_SCOPE_COLUMN, None)
        if client_id is not None and client_id not in principal.client_ids:
            raise ClientScopeViolationError(entity=type(row).__name__)
