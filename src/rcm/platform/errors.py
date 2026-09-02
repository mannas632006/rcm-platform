"""Error taxonomy and RFC 7807 Problem Details (M0.6, FR-API-04).

Every failure this system produces is a named type. That is the point of
CLAUDE.md section 0: *"When uncertain, raise a named exception. Never substitute a
default."* A generic `Exception` reaching a handler is a defect, because it tells
an operator nothing about what to do next -- and in a system where a wrong number
is a compliance event, "something went wrong" is not an outcome anyone can act on.

Two properties every error here has:

- **A machine-readable `code`**, which becomes the `type` URI. Clients branch on
  the code, never on the human-readable title, which is free to change.
- **Structured, scrubbed `context`.** Detail strings are fixed per class and
  contain no interpolated data. Anything variable goes in `context`, which passes
  through the PHI scrubber before serialisation (PD-13). An error payload is one
  of the four leak paths in CLAUDE.md section 6.3.

`detail` is deliberately not a formatted message. If an error needs to say which
claim failed, the claim id goes in `context["claim_id"]`, not into prose that a
future reader might extend with a patient name.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, ClassVar, Final

from rcm.platform.logging import scrub

#: Base for `type` URIs. Dereferenceable documentation lives at `docs/errors.md`.
PROBLEM_TYPE_BASE: Final = "https://errors.rcm.internal/"


class PlatformError(Exception):
    """Base for every error this platform raises deliberately.

    Subclasses set `code`, `title` and `status`. Instances carry optional
    structured context.
    """

    code: ClassVar[str] = "internal-error"
    title: ClassVar[str] = "Internal error"
    status: ClassVar[int] = HTTPStatus.INTERNAL_SERVER_ERROR
    detail: ClassVar[str] = "An unexpected internal error occurred."

    #: Whether this error class suspends the automation path that raised it
    #: (FR-BE-07). Compliance-class errors do; transient ones do not.
    suspends_automation: ClassVar[bool] = False

    def __init__(self, **context: Any) -> None:
        # The message is the class's fixed detail, never interpolated context.
        # `rcm.platform.logging` withholds exception messages anyway; this keeps
        # them harmless even where something else formats them.
        super().__init__(self.detail)
        self.context: dict[str, Any] = context

    @property
    def type_uri(self) -> str:
        return f"{PROBLEM_TYPE_BASE}{self.code}"

    def to_problem(
        self, *, instance: str | None = None, request_id: str | None = None
    ) -> dict[str, Any]:
        """Render as an RFC 7807 Problem Details document.

        Context is scrubbed here rather than at construction so the raising code
        can attach whatever it has without needing to know the rules.
        """
        problem: dict[str, Any] = {
            "type": self.type_uri,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
        }
        if instance is not None:
            problem["instance"] = instance
        if request_id is not None:
            # FR-API-03: every response correlates to logs and audit events.
            problem["request_id"] = request_id
        if self.context:
            scrubbed = scrub(self.context)
            problem["context"] = scrubbed
        return problem


# ---------------------------------------------------------------------------
# Client errors
# ---------------------------------------------------------------------------


class ValidationFailedError(PlatformError):
    code = "validation-failed"
    title = "Request failed validation"
    status = HTTPStatus.UNPROCESSABLE_ENTITY
    detail = "The request was well-formed but failed domain validation."


class AuthenticationFailedError(PlatformError):
    code = "authentication-failed"
    title = "Authentication failed"
    status = HTTPStatus.UNAUTHORIZED
    detail = "Valid credentials are required."


class AuthorizationDeniedError(PlatformError):
    code = "authorization-denied"
    title = "Authorization denied"
    status = HTTPStatus.FORBIDDEN
    detail = "The authenticated principal may not perform this action."


class ClientScopeViolationError(AuthorizationDeniedError):
    """A principal reached for data outside its client scope (PD-20, SR-21).

    Deliberately a 403 with the same body as any other authorisation denial: a
    404-vs-403 distinction here would let a caller probe which claim ids exist in
    another tenant.
    """

    code = "client-scope-violation"
    title = "Authorization denied"
    suspends_automation = True


class ResourceNotFoundError(PlatformError):
    code = "resource-not-found"
    title = "Resource not found"
    status = HTTPStatus.NOT_FOUND
    detail = "No resource matches the identifier supplied."


class IdempotencyKeyRequiredError(PlatformError):
    """PD-14: mutating endpoints require the header."""

    code = "idempotency-key-required"
    title = "Idempotency-Key header required"
    status = HTTPStatus.BAD_REQUEST
    detail = "Mutating requests must supply an Idempotency-Key header."


class IdempotencyConflictError(PlatformError):
    """The same key was reused with a different request body.

    Returning the stored response would be wrong -- the caller asked for
    something else. Failing is the only safe answer (PD-18).
    """

    code = "idempotency-conflict"
    title = "Idempotency key reused with a different payload"
    status = HTTPStatus.CONFLICT
    detail = "This Idempotency-Key was already used for a different request."


class RequestInProgressError(PlatformError):
    code = "request-in-progress"
    title = "An identical request is in progress"
    status = HTTPStatus.CONFLICT
    detail = "A request with this Idempotency-Key is currently being processed."


class RateLimitExceededError(PlatformError):
    code = "rate-limit-exceeded"
    title = "Rate limit exceeded"
    status = HTTPStatus.TOO_MANY_REQUESTS
    detail = "Too many requests. Retry after the interval indicated."


class PayloadTooLargeError(PlatformError):
    code = "payload-too-large"
    title = "Payload too large"
    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    detail = "The request body exceeds the configured maximum."


# ---------------------------------------------------------------------------
# Domain and state errors
# ---------------------------------------------------------------------------


class StateTransitionError(PlatformError):
    """An undefined claim state transition was attempted (FR-BE-09).

    Transitions are declared as data; anything not declared is rejected rather
    than tolerated.
    """

    code = "invalid-state-transition"
    title = "Invalid state transition"
    status = HTTPStatus.CONFLICT
    detail = "The requested transition is not defined for the entity's current state."


class ConcurrencyConflictError(PlatformError):
    code = "concurrency-conflict"
    title = "Concurrent modification"
    status = HTTPStatus.CONFLICT
    detail = "The entity was modified concurrently. Retry with fresh state."


class WorkItemLockedError(PlatformError):
    """M3.3: two operators cannot hold one item."""

    code = "work-item-locked"
    title = "Work item already assigned"
    status = HTTPStatus.CONFLICT
    detail = "Another operator holds an exclusive lock on this work item."


# ---------------------------------------------------------------------------
# Ingestion errors
# ---------------------------------------------------------------------------


class QuarantineError(PlatformError):
    """A file failed a data quality gate and was quarantined (DR-10).

    DR-10 requires a *named* alert with the specific failing assertion. The
    assertion goes in `context["assertion"]`; silent skipping is prohibited, and
    so is a generic message.
    """

    code = "file-quarantined"
    title = "Inbound file quarantined"
    status = HTTPStatus.UNPROCESSABLE_ENTITY
    detail = "The file failed a data quality gate and was quarantined unparsed."
    suspends_automation = True


class BalanceAssertionError(QuarantineError):
    """An 835's amounts do not reconcile (IR-02, M1.3).

    Never partially loaded. A file that does not balance is a file we do not
    understand, and posting part of it puts wrong numbers in the ledger.
    """

    code = "file-does-not-balance"
    title = "Remittance file does not balance"


class MalformedX12Error(QuarantineError):
    """Structural failure parsing untrusted EDI input (S-12)."""

    code = "malformed-x12"
    title = "Malformed X12 file"


class X12LimitExceededError(QuarantineError):
    """Size or nesting limit hit while parsing (S-12).

    A parser without limits is a denial-of-service surface, because the file
    arrives from outside.
    """

    code = "x12-limit-exceeded"
    title = "X12 file exceeds parsing limits"


# ---------------------------------------------------------------------------
# Fail-closed and compliance
# ---------------------------------------------------------------------------


class UncertaintyEscalationError(PlatformError):
    """PD-18: the system does not know, so it does not act.

    Raised at any decision point where the inputs do not determine an outcome.
    The claim stops and a human is asked. Substituting a default here is the
    exact failure CLAUDE.md section 0 calls a compliance event.
    """

    code = "escalated-uncertain"
    title = "Escalated: outcome uncertain"
    status = HTTPStatus.CONFLICT
    detail = "The system could not determine a safe outcome and escalated for review."
    suspends_automation = True


class ComplianceError(PlatformError):
    """A compliance-class failure. Suspends the affected automation path (FR-BE-07).

    Distinct from a validation failure: this is not the request being wrong, it is
    the system having done, or being about to do, something it must not.
    """

    code = "compliance-violation"
    title = "Compliance violation"
    status = HTTPStatus.INTERNAL_SERVER_ERROR
    detail = "A compliance control was violated. The affected automation is suspended."
    suspends_automation = True


class StalePayerRuleError(PlatformError):
    """A payer registry entry too old to drive a BLOCKING decision (BR-REG-02)."""

    code = "stale-payer-rule"
    title = "Payer rule is stale"
    status = HTTPStatus.CONFLICT
    detail = "The payer registry entry has not been verified recently enough to block."
    suspends_automation = True


class ManualCodingRequiredError(PlatformError):
    """PD-16: modifiers 25 and 59 are never auto-applied.

    The refusal is an outcome, not an error to be worked around. These modifiers
    assert clinical facts about the encounter that no rule can establish.
    """

    code = "manual-coding-required"
    title = "Manual coding required"
    status = HTTPStatus.CONFLICT
    detail = "This correction requires a qualified human coder and cannot be automated."


# ---------------------------------------------------------------------------
# External systems
# ---------------------------------------------------------------------------


class ExternalSystemError(PlatformError):
    code = "external-system-error"
    title = "External system error"
    status = HTTPStatus.BAD_GATEWAY
    detail = "An external system returned an error."


class ExternalSystemUnavailableError(ExternalSystemError):
    """Both primary and secondary failed (M3.7). Named, and no data lost."""

    code = "external-system-unavailable"
    title = "External system unavailable"
    status = HTTPStatus.SERVICE_UNAVAILABLE
    detail = "No route to the external system is currently available."
    suspends_automation = True


#: Every error class, keyed by code. `docs/errors.md` is generated from this and
#: `tests/unit/test_error_taxonomy.py` asserts the two never drift.
def all_error_classes() -> list[type[PlatformError]]:
    """Every concrete error type, depth-first, deduplicated, in definition order."""
    seen: dict[str, type[PlatformError]] = {}

    def walk(cls: type[PlatformError]) -> None:
        if cls.code not in seen:
            seen[cls.code] = cls
        for sub in cls.__subclasses__():
            walk(sub)

    walk(PlatformError)
    return list(seen.values())
