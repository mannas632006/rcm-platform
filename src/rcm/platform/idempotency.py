"""Idempotency for mutating endpoints (M0.8, PD-14, FR-API-05).

    PD-14: Every mutating endpoint accepts `Idempotency-Key`. Retries never duplicate.

A retry in this system is not a hypothetical. Operators work on unreliable
connections, the frontend retries on timeout, and a clearinghouse adapter retries
on a 502. If the first attempt reached the database and the response was lost,
the second attempt must return the first attempt's answer -- not submit the claim
again. A duplicate submission is a duplicate bill.

The design is the one Stripe settled on, with the parts that matter here:

- **The key is scoped to the principal**, not global. Two tenants choosing the
  same UUID must not collide, and one tenant must not be able to probe another's
  keys by guessing.
- **The request body is fingerprinted.** Reusing a key with a different body is
  a client bug and returns 409, because replaying the stored response would
  answer a question the caller did not ask.
- **A concurrent duplicate gets 409, not a second execution.** The reservation is
  claimed atomically before the handler runs; the loser is told the request is in
  flight rather than racing it.
- **Only success is replayed.** A 500 is not a result worth making permanent --
  the condition that caused it may be transient, and freezing it would turn one
  bad minute into a permanently broken key.

Redis holds the records. It is the right store because every entry has a TTL and
none of it is a system of record: losing Redis costs idempotency protection for
the TTL window, not data. The record holds a response body, so it can hold PHI --
which is why `idempotency:` keys carry the same retention limit as a session and
are never logged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

import redis.asyncio as aioredis
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from rcm.platform.config import Settings, get_settings
from rcm.platform.errors import (
    IdempotencyConflictError,
    IdempotencyKeyRequiredError,
    PlatformError,
    RequestInProgressError,
)
from rcm.platform.logging import get_logger

log = get_logger(__name__)

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
REPLAYED_HEADER: Final = "Idempotency-Replayed"

#: Methods that change state and therefore require a key.
MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths exempt from the requirement. Each entry is a deliberate decision:
#: `/health` has no effect to duplicate, and the token endpoint is the one call a
#: client makes before it has anything to be idempotent about.
EXEMPT_PATHS: Final[frozenset[str]] = frozenset({"/health", "/ready", "/auth/token"})

#: How long a reservation may be held before it is assumed abandoned. Shorter
#: than the record TTL: a crashed handler must not block its own retry for a day.
RESERVATION_TTL_SECONDS: Final = 120

_STATE_IN_PROGRESS: Final = "in_progress"
_STATE_COMPLETE: Final = "complete"


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """A completed response, replayable for the TTL window."""

    status_code: int
    body: bytes
    media_type: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "status_code": self.status_code,
                "body": self.body.decode("utf-8", errors="replace"),
                "media_type": self.media_type,
            }
        )

    @staticmethod
    def from_json(raw: str) -> StoredResponse:
        data = json.loads(raw)
        return StoredResponse(
            status_code=int(data["status_code"]),
            body=data["body"].encode("utf-8"),
            media_type=str(data["media_type"]),
        )


def fingerprint(method: str, path: str, body: bytes) -> str:
    """Stable digest of what the caller asked for.

    Method and path are included so one key cannot be reused across two
    different operations -- a client that reuses a key for `POST /claims` and
    then `DELETE /claims/1` has a bug, and this surfaces it as a 409 rather than
    replaying a creation in answer to a deletion.
    """
    digest = hashlib.sha256()
    digest.update(method.encode())
    digest.update(b"\x00")
    digest.update(path.encode())
    digest.update(b"\x00")
    digest.update(body)
    return digest.hexdigest()


def record_key(*, principal_subject: str, idempotency_key: str) -> str:
    """Redis key, namespaced per principal.

    The principal's subject is hashed rather than embedded: Redis keys turn up in
    `SCAN` output, slow-log entries and monitoring dashboards, none of which are
    inside the PHI boundary (PD-13).
    """
    subject_digest = hashlib.sha256(principal_subject.encode()).hexdigest()[:32]
    key_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    return f"idempotency:{subject_digest}:{key_digest}"


class IdempotencyStore:
    """Redis-backed reservation and replay store."""

    def __init__(self, client: aioredis.Redis, *, ttl_seconds: int) -> None:
        self._redis = client
        self._ttl = ttl_seconds

    async def reserve(self, key: str, request_fingerprint: str) -> StoredResponse | None:
        """Claim `key`, or return the stored response if it is already complete.

        The claim is a single `SET NX`, so two concurrent requests cannot both
        win. Doing this as GET-then-SET would leave a window in which both
        callers see nothing and both execute -- which is the exact duplicate
        PD-14 exists to prevent.
        """
        claimed = await self._redis.set(
            key,
            json.dumps({"state": _STATE_IN_PROGRESS, "fingerprint": request_fingerprint}),
            nx=True,
            ex=RESERVATION_TTL_SECONDS,
        )
        if claimed:
            return None

        raw = await self._redis.get(key)
        if raw is None:
            # The reservation expired between SET NX and GET. Treat as a fresh
            # request: re-running is safer than failing a legitimate retry.
            return None

        existing = json.loads(raw)
        if existing.get("fingerprint") != request_fingerprint:
            raise IdempotencyConflictError()
        if existing.get("state") == _STATE_IN_PROGRESS:
            raise RequestInProgressError()
        return StoredResponse.from_json(existing["response"])

    async def complete(self, key: str, request_fingerprint: str, response: StoredResponse) -> None:
        """Store a successful response for replay."""
        await self._redis.set(
            key,
            json.dumps(
                {
                    "state": _STATE_COMPLETE,
                    "fingerprint": request_fingerprint,
                    "response": response.to_json(),
                }
            ),
            ex=self._ttl,
        )

    async def release(self, key: str) -> None:
        """Drop a reservation whose handler failed.

        A failed attempt leaves no record, so the caller may retry immediately.
        Persisting the failure would make a transient outage permanent for the
        lifetime of the key.
        """
        await self._redis.delete(key)


async def _read_body(response: Response) -> bytes:
    """Drain a response body, whatever shape the middleware stack produced.

    `BaseHTTPMiddleware` hands back a private `_StreamingResponse` that is a
    plain `Response` subclass rather than `StreamingResponse`, so an isinstance
    check against the public class silently misses it -- and a body that is
    never drained is a reservation that is never completed, which turns the
    second attempt of every retry into a 409.

    Duck-typing on `body_iterator` is therefore deliberate: it matches what the
    object actually is rather than what the public type hierarchy suggests.
    """
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return bytes(getattr(response, "body", b""))

    sections: list[bytes] = []
    async for section in iterator:
        sections.append(section.encode() if isinstance(section, str) else bytes(section))
    return b"".join(sections)


def _principal_subject(request: Request, settings: Settings) -> str:
    """Identify the caller, for key namespacing.

    This middleware has to authenticate the request *itself*. Reading
    `request.state.principal` does not work: ASGI middleware runs before
    routing, so the `current_principal` dependency has not executed yet and the
    attribute is always absent. An earlier version did exactly that, every key
    landed in a shared "anonymous" namespace, and two tenants using the same
    key value replayed each other's responses -- a cross-tenant disclosure
    through the idempotency cache, which is the leak PD-20 exists to prevent
    arriving by a route PD-20 does not cover.

    The verified principal is cached on `request.state` so the dependency does
    not verify the same token a second time.

    An unverifiable token yields `"anonymous"`, which is safe: the request is
    about to be rejected by the auth dependency, the response will not be 2xx,
    and a non-2xx response releases the reservation rather than storing it.
    """
    from rcm.platform.auth import principal_from_token
    from rcm.platform.errors import AuthenticationFailedError

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return "anonymous"

    try:
        principal = principal_from_token(token, settings)
    except AuthenticationFailedError:
        return "anonymous"

    request.state.principal = principal
    return principal.subject


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """ASGI middleware enforcing PD-14 on every mutating route.

    Implemented as middleware rather than a per-route dependency on purpose: a
    dependency is something fifty future endpoints must each remember to add,
    and the one that forgets is the one that double-submits a claim. Enforcing it
    centrally means a new endpoint is covered the moment it is registered, and
    opting out requires editing `EXEMPT_PATHS`, which is a visible diff.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: IdempotencyStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(app)
        self._store = store
        self._settings = settings

    def _settings_or_default(self) -> Settings:
        return self._settings or get_settings()

    async def _store_or_default(self) -> IdempotencyStore:
        if self._store is None:
            settings = self._settings_or_default()
            # redis-py ships `from_url` without annotations; the return type is
            # documented and stable, so it is narrowed here rather than left as Any.
            client: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                settings.redis_url.get_secret_value(), decode_responses=True
            )
            self._store = IdempotencyStore(client, ttl_seconds=settings.idempotency_key_ttl_seconds)
        return self._store

    @staticmethod
    def _problem_response(request: Request, exc: PlatformError) -> Response:
        """Render a `PlatformError` raised inside the middleware.

        Starlette installs its exception handlers *below* user middleware, so an
        exception raised here never reaches the app's RFC 7807 handler -- it
        escapes as a bare 500. Without this, a request missing an
        `Idempotency-Key` would be answered with an opaque internal error
        instead of the 400 that tells the client what to fix.
        """
        from starlette.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status,
            content=exc.to_problem(
                instance=request.url.path,
                request_id=getattr(request.state, "request_id", None),
            ),
            media_type="application/problem+json",
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await self._dispatch(request, call_next)
        except PlatformError as exc:
            return self._problem_response(request, exc)

    async def _dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in MUTATING_METHODS or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        key_header = request.headers.get(IDEMPOTENCY_HEADER)
        if not key_header:
            raise IdempotencyKeyRequiredError(method=request.method, path=request.url.path)

        body = await request.body()
        request_fingerprint = fingerprint(request.method, request.url.path, body)
        settings = self._settings_or_default()
        key = record_key(
            principal_subject=_principal_subject(request, settings),
            idempotency_key=key_header,
        )
        store = await self._store_or_default()

        stored = await store.reserve(key, request_fingerprint)
        if stored is not None:
            log.info("idempotency.replayed", path=request.url.path, status=stored.status_code)
            response = Response(
                content=stored.body,
                status_code=stored.status_code,
                media_type=stored.media_type,
            )
            response.headers[REPLAYED_HEADER] = "true"
            return response

        try:
            response = await call_next(request)
        except Exception:
            await store.release(key)
            raise

        # Only 2xx is made permanent. A 4xx is the caller's to fix and a 5xx may
        # be transient; freezing either would deny the caller a working retry.
        if HTTPStatus.OK <= response.status_code < HTTPStatus.MULTIPLE_CHOICES:
            payload = await _read_body(response)
            await store.complete(
                key,
                request_fingerprint,
                StoredResponse(
                    status_code=response.status_code,
                    body=payload,
                    media_type=response.media_type or "application/json",
                ),
            )
            replayable = Response(
                content=payload,
                status_code=response.status_code,
                media_type=response.media_type or "application/json",
            )
            replayable.raw_headers = [
                *(h for h in response.raw_headers if h[0].lower() != b"content-length"),
            ]
            replayable.headers["content-length"] = str(len(payload))
            return replayable

        await store.release(key)
        return response
