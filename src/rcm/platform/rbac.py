"""Roles and permissions (S-4, SR-21, SR-22, SRS section 11.5).

The role matrix is **data**, not conditionals scattered through handlers. A
permission question has exactly one answer here, and a reviewer can read the whole
authorisation model in one table rather than reconstructing it from forty `if`
statements.

Two things this module does not do, deliberately:

- It does not enforce **client scope**. That is `rcm.platform.scoping`, and it
  runs at the query layer (PD-20, SR-21) precisely so that forgetting to call
  this module cannot leak another tenant's rows.
- It does not decide **write-off thresholds**. Those are amounts, and amounts are
  domain policy; this module answers only whether the role may write off at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID


class Role(StrEnum):
    """The nine roles in SRS section 11.5. No role is implied by another."""

    AR_SPECIALIST = "AR_SPECIALIST"
    DENIAL_SPECIALIST = "DENIAL_SPECIALIST"
    CODER = "CODER"
    PAYMENT_POSTER = "PAYMENT_POSTER"
    OPS_MANAGER = "OPS_MANAGER"
    RULES_AUTHOR = "RULES_AUTHOR"
    ENGINEER = "ENGINEER"
    SPONSOR = "SPONSOR"
    AUDITOR = "AUDITOR"


class Resource(StrEnum):
    """The permission axes of the SRS section 11.5 matrix."""

    CLAIMS = "CLAIMS"
    CODING = "CODING"
    RULES = "RULES"
    REGISTRY = "REGISTRY"
    MODELS = "MODELS"
    USERS = "USERS"
    WRITE_OFF = "WRITE_OFF"


class Access(StrEnum):
    NONE = "NONE"
    READ = "READ"
    READ_WRITE = "READ_WRITE"


class ClientScope(StrEnum):
    ALL = "ALL"
    ASSIGNED_ONLY = "ASSIGNED_ONLY"


_N = Access.NONE
_R = Access.READ
_RW = Access.READ_WRITE

#: SRS section 11.5, transcribed. Every cell is explicit -- there is no "unspecified"
#: that a future reader could resolve optimistically.
ROLE_MATRIX: Final[dict[Role, dict[Resource, Access]]] = {
    Role.AR_SPECIALIST: {
        Resource.CLAIMS: _RW,
        Resource.CODING: _N,
        Resource.RULES: _R,
        Resource.REGISTRY: _R,
        Resource.MODELS: _N,
        Resource.USERS: _N,
        Resource.WRITE_OFF: _RW,  # bounded by an amount threshold, see below
    },
    Role.DENIAL_SPECIALIST: {
        Resource.CLAIMS: _RW,
        Resource.CODING: _N,
        Resource.RULES: _R,
        Resource.REGISTRY: _R,
        Resource.MODELS: _N,
        Resource.USERS: _N,
        Resource.WRITE_OFF: _RW,
    },
    Role.CODER: {
        Resource.CLAIMS: _R,
        Resource.CODING: _RW,
        Resource.RULES: _N,
        Resource.REGISTRY: _R,
        Resource.MODELS: _N,
        Resource.USERS: _N,
        Resource.WRITE_OFF: _N,
    },
    Role.PAYMENT_POSTER: {
        Resource.CLAIMS: _RW,
        Resource.CODING: _N,
        Resource.RULES: _N,
        Resource.REGISTRY: _R,
        Resource.MODELS: _N,
        Resource.USERS: _N,
        Resource.WRITE_OFF: _N,
    },
    Role.OPS_MANAGER: {
        Resource.CLAIMS: _RW,
        Resource.CODING: _R,
        Resource.RULES: _R,
        Resource.REGISTRY: _RW,
        Resource.MODELS: _R,
        Resource.USERS: _N,
        Resource.WRITE_OFF: _RW,
    },
    Role.RULES_AUTHOR: {
        Resource.CLAIMS: _R,
        Resource.CODING: _N,
        Resource.RULES: _RW,
        Resource.REGISTRY: _RW,
        Resource.MODELS: _R,
        Resource.USERS: _N,
        Resource.WRITE_OFF: _N,
    },
    Role.ENGINEER: {
        Resource.CLAIMS: _RW,
        Resource.CODING: _R,
        Resource.RULES: _RW,
        Resource.REGISTRY: _RW,
        Resource.MODELS: _RW,
        Resource.USERS: _R,
        Resource.WRITE_OFF: _N,
    },
    Role.SPONSOR: {
        Resource.CLAIMS: _R,
        Resource.CODING: _R,
        Resource.RULES: _R,
        Resource.REGISTRY: _R,
        Resource.MODELS: _RW,
        Resource.USERS: _RW,
        Resource.WRITE_OFF: _RW,
    },
    # SR-22: the Auditor has no write capability anywhere, including notes.
    # Its access is separately logged and reviewed.
    Role.AUDITOR: {
        Resource.CLAIMS: _R,
        Resource.CODING: _R,
        Resource.RULES: _R,
        Resource.REGISTRY: _R,
        Resource.MODELS: _R,
        Resource.USERS: _R,
        Resource.WRITE_OFF: _N,
    },
}

#: Roles limited to the practices they are assigned. The others see all clients.
_ASSIGNED_ONLY: Final[frozenset[Role]] = frozenset(
    {Role.AR_SPECIALIST, Role.DENIAL_SPECIALIST, Role.CODER, Role.PAYMENT_POSTER}
)

#: Roles whose write-off authority is capped by an amount rather than unlimited.
#: The cap itself is TBD-48; this only records which roles it applies to.
WRITE_OFF_THRESHOLD_BOUND: Final[frozenset[Role]] = frozenset(
    {Role.AR_SPECIALIST, Role.DENIAL_SPECIALIST}
)

#: SR-22 again, as an assertion the tests can check directly.
READ_ONLY_ROLES: Final[frozenset[Role]] = frozenset({Role.AUDITOR})


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting, with what roles, over which practices.

    `practice_ids` is meaningful only when `client_scope` is ASSIGNED_ONLY. For an
    ALL-scope principal it is empty, and `rcm.platform.scoping` applies no filter.
    """

    subject: str
    roles: frozenset[Role]
    practice_ids: frozenset[UUID] = field(default_factory=frozenset)
    session_id: UUID | None = None

    @property
    def client_scope(self) -> ClientScope:
        """ALL if any role grants it.

        Roles are additive: someone who is both an Ops Manager and an AR
        Specialist sees everything the wider role allows. Taking the narrower
        scope instead would silently hide data from a person entitled to it,
        which reads as a bug and gets "fixed" by granting more.
        """
        if any(role not in _ASSIGNED_ONLY for role in self.roles):
            return ClientScope.ALL
        return ClientScope.ASSIGNED_ONLY

    @property
    def is_read_only(self) -> bool:
        """True when every role held is read-only (SR-22)."""
        return bool(self.roles) and all(role in READ_ONLY_ROLES for role in self.roles)

    def access_to(self, resource: Resource) -> Access:
        """Highest access any held role grants to `resource`.

        A read-only role never widens access: an Auditor who somehow also held a
        writing role would be a misconfiguration, and `is_read_only` reports it,
        but the matrix answer stays the union so behaviour is predictable.
        """
        best = Access.NONE
        for role in self.roles:
            granted = ROLE_MATRIX[role][resource]
            if granted is Access.READ_WRITE:
                return Access.READ_WRITE
            if granted is Access.READ:
                best = Access.READ
        return best

    def can_read(self, resource: Resource) -> bool:
        return self.access_to(resource) in (Access.READ, Access.READ_WRITE)

    def can_write(self, resource: Resource) -> bool:
        return self.access_to(resource) is Access.READ_WRITE
