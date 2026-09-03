"""Idempotency middleware (PD-14, FR-API-01).

Applies to every mutating request, so PD-14's "every mutating endpoint" is a
property of the application rather than a checklist each new route has to
remember. A route that forgets is not possible: forgetting would mean removing
this middleware, which the test suite catches.

`GET` and `HEAD` pass straight through -- they change nothing, so replaying them
is free.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from rcm.platform.errors import (
    IdempotencyConflictError,
    IdempotencyKeyRequiredError,
    PayloadTooLargeError,
    RequestInProgressError,
)
from rcm.platform.idempotency import (
    MUTATING_METHODS,
    IdempotencyRecord,
    IdempotencyStore,
    RecordState,
    fingerprint,
)
from rcm.platform.logging import get_logger

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
PROBLEM_CONTENT_TYPE = "application/problem+json"

log = get_logger(__name__)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        store: IdempotencyStore,
        ttl_seconds: int,
        max_body_bytes: int,
    ) -> None:
        super().__init__(app)
        self._store = store
        self._ttl = ttl_seconds
        self._max_body = max_body_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method.upper() not in MUTATING_METHODS:
            return await call_next(request)

        key = request.headers.get(IDEMPOTENCY_HEADER)
        if not key:
            return self._problem(request, IdempotencyKeyRequiredError())

        body = await request.body()
        if len(body) > self._max_body:
            return self._problem(request, PayloadTooLargeError())

        request_fingerprint = fingerprint(request.method, request.url.path, body)
        pending = IdempotencyRecord(
            state=RecordState.IN_PROGRESS, request_fingerprint=request_fingerprint
        )

        if not self._store.claim(key, pending, self._ttl):
            existing = self._store.get(key)
            if existing is None:
                # The record expired between claim and get. Treating this as a
                # fresh request is the safe reading: the TTL has passed, so the
                # original response is no longer replayable either way.
                return await self._execute_and_record(request, call_next, key, request_fingerprint)

            if existing.request_fingerprint != request_fingerprint:
                # Same key, different request. Replaying the stored response
                # would tell the caller their new request succeeded when it was
                # never performed.
                return self._problem(request, IdempotencyConflictError())

            if existing.state is RecordState.IN_PROGRESS:
                return self._problem(request, RequestInProgressError())

            return self._replay(existing)

        return await self._execute_and_record(request, call_next, key, request_fingerprint)

    async def _execute_and_record(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
        key: str,
        request_fingerprint: str,
    ) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            # The attempt failed in a way we did not model. Releasing the key
            # lets the client retry; holding it would block a legitimate retry
            # for the whole TTL over a transient fault.
            self._store.release(key)
            raise

        body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]

        # Only successful responses are replayable. Recording a 500 would pin the
        # client to a failure it could otherwise retry past.
        if 200 <= response.status_code < 300:
            self._store.complete(
                key,
                IdempotencyRecord(
                    state=RecordState.COMPLETED,
                    request_fingerprint=request_fingerprint,
                    status_code=response.status_code,
                    body=body.decode("utf-8", errors="replace"),
                ),
                self._ttl,
            )
        else:
            self._store.release(key)

        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    @staticmethod
    def _replay(record: IdempotencyRecord) -> Response:
        log.info("idempotency.replayed", status_code=record.status_code)
        return Response(
            content=record.body or "",
            status_code=record.status_code or 200,
            media_type="application/json",
            headers={REPLAYED_HEADER: "true"},
        )

    @staticmethod
    def _problem(request: Request, error: Exception) -> JSONResponse:
        from rcm.platform.errors import PlatformError

        assert isinstance(error, PlatformError)
        return JSONResponse(
            status_code=error.status,
            content=error.to_problem(
                instance=request.url.path,
                request_id=getattr(request.state, "request_id", None),
            ),
            media_type=PROBLEM_CONTENT_TYPE,
        )
