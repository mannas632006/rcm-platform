"""Roles, permissions and token verification (M0.7; S-3, S-4).

The role matrix is a security control that looks like a dictionary, which is
exactly why it needs tests: a permission added to the wrong row is a one-token
diff that no reviewer reliably catches, and the consequence is a coder able to
sign appeals or an administrator able to read claims.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
import pytest
from pydantic import SecretStr

from rcm.platform.auth import (
    ROLE_PERMISSIONS,
    Permission,
    Principal,
    Role,
    issue_dev_token,
    principal_from_token,
)
from rcm.platform.config import Settings
from rcm.platform.errors import AuthenticationFailedError, AuthorizationDeniedError

CLIENT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")


@pytest.fixture
def settings() -> Settings:
    # 32 bytes: shorter keys are refused by validate_for_environment and warned
    # about by PyJWT, and `filterwarnings = ["error"]` makes that a test failure.
    return Settings(
        environment="ci",
        dev_jwt_secret=SecretStr("ci-only-test-signing-key-32bytes"),
    )


def token_for(settings: Settings, *roles: Role, **kwargs: object) -> str:
    return issue_dev_token(
        subject="user-1",
        roles=list(roles),
        client_ids=[CLIENT],
        settings=settings,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------- role matrix


def test_every_role_has_at_least_one_permission() -> None:
    """A role with no permissions is a provisioning trap, not a security measure."""
    for role in Role:
        assert ROLE_PERMISSIONS[role], f"{role} grants nothing"


def test_every_permission_is_reachable_by_some_role() -> None:
    """An unreachable permission means a route nobody can call."""
    granted = {p for perms in ROLE_PERMISSIONS.values() for p in perms}
    unreachable = set(Permission) - granted
    assert not unreachable, f"no role grants: {sorted(str(p) for p in unreachable)}"


@pytest.mark.parametrize(
    ("role", "permission", "expected"),
    [
        # Signing an appeal is a legal act. Only the two roles accountable for
        # the denial outcome hold it (M5.7).
        (Role.DENIAL_SPECIALIST, Permission.APPEAL_SIGN, True),
        (Role.OPERATIONS_MANAGER, Permission.APPEAL_SIGN, True),
        (Role.AR_SPECIALIST, Permission.APPEAL_SIGN, False),
        (Role.CODER, Permission.APPEAL_SIGN, False),
        # Authoring a rule and switching it on for every client are separate
        # acts (M4.5), but both sit with the rules author; nobody else promotes.
        (Role.RULES_AUTHOR, Permission.RULE_PROMOTE, True),
        (Role.OPERATIONS_MANAGER, Permission.RULE_PROMOTE, False),
        # Minimum necessary: administering users is not a clinical role.
        (Role.ADMINISTRATOR, Permission.CLAIM_READ, False),
        (Role.ADMINISTRATOR, Permission.USER_ADMIN, True),
        # Only coders write codes.
        (Role.CODER, Permission.CODING_WRITE, True),
        (Role.AR_SPECIALIST, Permission.CODING_WRITE, False),
        (Role.OPERATIONS_MANAGER, Permission.CODING_WRITE, False),
    ],
)
def test_role_matrix(role: Role, permission: Permission, expected: bool) -> None:
    principal = Principal(subject="u", roles=frozenset({role}), client_ids=frozenset({CLIENT}))
    assert principal.has(permission) is expected


def test_require_raises_for_a_missing_permission() -> None:
    principal = Principal(
        subject="u", roles=frozenset({Role.CODER}), client_ids=frozenset({CLIENT})
    )
    principal.require(Permission.CODING_WRITE)
    with pytest.raises(AuthorizationDeniedError):
        principal.require(Permission.APPEAL_SIGN)


def test_permissions_are_the_union_of_several_roles() -> None:
    principal = Principal(
        subject="u",
        roles=frozenset({Role.CODER, Role.DENIAL_SPECIALIST}),
        client_ids=frozenset({CLIENT}),
    )
    assert principal.has(Permission.CODING_WRITE)
    assert principal.has(Permission.APPEAL_SIGN)


def test_authorization_denial_carries_no_principal_detail() -> None:
    """An error payload is a PHI leak path (PD-13); a role list fingerprints a person."""
    principal = Principal(
        subject="maria.gonzalez@clinic.example",
        roles=frozenset({Role.CODER}),
        client_ids=frozenset({CLIENT}),
    )
    with pytest.raises(AuthorizationDeniedError) as caught:
        principal.require(Permission.APPEAL_SIGN)

    rendered = str(caught.value.to_problem())
    assert "maria" not in rendered.lower()
    assert "CODER" not in rendered


# --------------------------------------------------------- token verification


def test_a_valid_token_becomes_a_principal(settings: Settings) -> None:
    principal = principal_from_token(token_for(settings, Role.AR_SPECIALIST), settings)

    assert principal.subject == "user-1"
    assert principal.roles == frozenset({Role.AR_SPECIALIST})
    assert principal.client_ids == frozenset({CLIENT})
    assert principal.is_system is False


def test_an_expired_token_is_rejected(settings: Settings) -> None:
    expired = token_for(settings, Role.AR_SPECIALIST, expires_in=timedelta(seconds=-60))
    with pytest.raises(AuthenticationFailedError):
        principal_from_token(expired, settings)


def test_a_token_signed_with_another_key_is_rejected(settings: Settings) -> None:
    forged = jwt.encode(
        {
            "sub": "attacker",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "iat": 0,
            "exp": 9_999_999_999,
            "roles": [str(Role.ADMINISTRATOR)],
            "client_ids": [str(CLIENT)],
        },
        "a-different-signing-key-of-32-bytes!",
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationFailedError):
        principal_from_token(forged, settings)


def test_a_token_for_another_audience_is_rejected(settings: Settings) -> None:
    other = Settings(
        environment="ci",
        dev_jwt_secret=settings.dev_jwt_secret,
        oidc_audience="some-other-service",
    )
    wrong_audience = token_for(other, Role.AR_SPECIALIST)
    with pytest.raises(AuthenticationFailedError):
        principal_from_token(wrong_audience, settings)


def test_a_token_with_no_recognised_role_is_rejected(settings: Settings) -> None:
    """Fail closed: an unknown role grants nothing, so the token is useless."""
    stranger = jwt.encode(
        {
            "sub": "user-1",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "iat": 0,
            "exp": 9_999_999_999,
            "roles": ["SUPREME_LEADER"],
            "client_ids": [str(CLIENT)],
        },
        settings.dev_jwt_secret.get_secret_value(),  # type: ignore[union-attr]
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationFailedError):
        principal_from_token(stranger, settings)


def test_every_rejection_looks_identical(settings: Settings) -> None:
    """No oracle: a caller cannot learn which check failed."""
    expired = token_for(settings, Role.CODER, expires_in=timedelta(seconds=-1))
    garbage = "not.a.token"

    problems = []
    for token in (expired, garbage):
        with pytest.raises(AuthenticationFailedError) as caught:
            principal_from_token(token, settings)
        problems.append(caught.value.to_problem())

    assert problems[0] == problems[1]


# ------------------------------------------------------------- the dev stub


def test_the_dev_stub_refuses_to_mint_tokens_outside_local_and_ci() -> None:
    """The stub cannot be switched on in a deployed environment."""
    deployed = Settings(environment="staging")
    with pytest.raises(RuntimeError, match="not available in 'staging'"):
        issue_dev_token(subject="u", roles=[Role.ADMINISTRATOR], client_ids=[], settings=deployed)


def test_a_deployed_environment_refuses_to_start_with_a_dev_secret() -> None:
    """Belt and braces: config validation rejects the combination outright."""
    unsafe = Settings(
        environment="production",
        dev_jwt_secret=SecretStr("a-32-byte-secret-for-this-test!!"),
        log_json=True,
    )
    with pytest.raises(ValueError, match="OIDC stub must never sign tokens"):
        unsafe.validate_for_environment()


def test_a_short_dev_secret_is_refused() -> None:
    """A weak HMAC key is refused rather than warned about (RFC 7518 section 3.2)."""
    weak = Settings(environment="local", dev_jwt_secret=SecretStr("too-short"))
    with pytest.raises(ValueError, match="at least 32 bytes"):
        weak.validate_for_environment()


def test_system_principals_are_marked(settings: Settings) -> None:
    principal = principal_from_token(token_for(settings, Role.SYSTEM), settings)
    assert principal.is_system is True
