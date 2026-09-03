"""S-4 / SR-22: the role matrix matches SRS section 11.5, cell for cell.

CLAUDE.md section 6.3 lists privilege escalation as a threat case: *"Each role attempts
every forbidden action."* That is 9 roles x 7 resources = 63 assertions, so the
expected matrix is transcribed here independently of the implementation. If both
were generated from the same source the test would only prove the code agrees with
itself.
"""

from __future__ import annotations

import pytest

from rcm.platform.rbac import (
    ROLE_MATRIX,
    Access,
    ClientScope,
    Principal,
    Resource,
    Role,
)

pytestmark = pytest.mark.security

_N, _R, _RW = "—", "R", "R/W"

#: SRS section 11.5, read off the document rather than off the code.
EXPECTED: dict[Role, dict[Resource, str]] = {
    #                     Claims Coding Rules Registry Models Users Write-off
    Role.AR_SPECIALIST: dict(zip(Resource, [_RW, _N, _R, _R, _N, _N, _RW], strict=True)),
    Role.DENIAL_SPECIALIST: dict(zip(Resource, [_RW, _N, _R, _R, _N, _N, _RW], strict=True)),
    Role.CODER: dict(zip(Resource, [_R, _RW, _N, _R, _N, _N, _N], strict=True)),
    Role.PAYMENT_POSTER: dict(zip(Resource, [_RW, _N, _N, _R, _N, _N, _N], strict=True)),
    Role.OPS_MANAGER: dict(zip(Resource, [_RW, _R, _R, _RW, _R, _N, _RW], strict=True)),
    Role.RULES_AUTHOR: dict(zip(Resource, [_R, _N, _RW, _RW, _R, _N, _N], strict=True)),
    Role.ENGINEER: dict(zip(Resource, [_RW, _R, _RW, _RW, _RW, _R, _N], strict=True)),
    Role.SPONSOR: dict(zip(Resource, [_R, _R, _R, _R, _RW, _RW, _RW], strict=True)),
    Role.AUDITOR: dict(zip(Resource, [_R, _R, _R, _R, _R, _R, _N], strict=True)),
}

_SYMBOL_TO_ACCESS = {_N: Access.NONE, _R: Access.READ, _RW: Access.READ_WRITE}

#: Roles whose client scope is "Assigned only" in SRS section 11.5.
ASSIGNED_ONLY_ROLES = {
    Role.AR_SPECIALIST,
    Role.DENIAL_SPECIALIST,
    Role.CODER,
    Role.PAYMENT_POSTER,
}


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("resource", list(Resource))
def test_matrix_cell_matches_the_srs(role: Role, resource: Resource) -> None:
    expected = _SYMBOL_TO_ACCESS[EXPECTED[role][resource]]
    assert ROLE_MATRIX[role][resource] is expected, (
        f"{role.value} x {resource.value}: SRS 11.5 says {EXPECTED[role][resource]}"
    )


def test_every_role_and_resource_is_covered() -> None:
    """No cell is left unspecified.

    A missing cell would be resolved by whatever the lookup does on a miss, and
    the safe answer and the convenient answer are not the same.
    """
    assert set(ROLE_MATRIX) == set(Role)
    for role, cells in ROLE_MATRIX.items():
        assert set(cells) == set(Resource), f"{role.value} is missing resources"


@pytest.mark.parametrize("resource", list(Resource))
def test_auditor_can_never_write(resource: Resource) -> None:
    """SR-22: the Auditor has no write capability anywhere, including notes."""
    auditor = Principal(subject="user:auditor", roles=frozenset({Role.AUDITOR}))
    assert not auditor.can_write(resource)
    assert auditor.is_read_only


@pytest.mark.parametrize("role", sorted(ASSIGNED_ONLY_ROLES, key=str))
def test_assigned_only_roles_are_scoped(role: Role) -> None:
    principal = Principal(subject="user:x", roles=frozenset({role}))
    assert principal.client_scope is ClientScope.ASSIGNED_ONLY


@pytest.mark.parametrize("role", sorted(set(Role) - ASSIGNED_ONLY_ROLES, key=str))
def test_all_scope_roles_see_every_client(role: Role) -> None:
    principal = Principal(subject="user:x", roles=frozenset({role}))
    assert principal.client_scope is ClientScope.ALL


def test_no_role_may_administer_users_except_sponsor_and_engineer() -> None:
    """Privilege escalation: user administration is the route to every other right."""
    for role in Role:
        access = ROLE_MATRIX[role][Resource.USERS]
        if role is Role.SPONSOR:
            assert access is Access.READ_WRITE
        elif role in (Role.ENGINEER, Role.AUDITOR):
            assert access is Access.READ
        else:
            assert access is Access.NONE, f"{role.value} can reach user administration"


def test_combined_roles_take_the_union() -> None:
    """Holding two roles grants the wider access, never a surprising intersection."""
    combined = Principal(subject="user:combo", roles=frozenset({Role.CODER, Role.PAYMENT_POSTER}))
    assert combined.can_write(Resource.CODING)  # from Coder
    assert combined.can_write(Resource.CLAIMS)  # from Payment Poster
    assert not combined.can_write(Resource.RULES)  # from neither
