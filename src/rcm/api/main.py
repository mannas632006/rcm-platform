"""FastAPI application (M0.3).

Wires the platform's cross-cutting behaviour before any route exists, because
every one of these is easier to add now than to retrofit onto fifty endpoints:

- a `request_id` on every response and every log line (FR-API-03),
- RFC 7807 Problem Details for every error (FR-API-04),
- a catch-all that converts an unexpected exception into a safe response rather
  than a stack trace (PD-13, PD-18).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from rcm.api.idempotency_middleware import IdempotencyMiddleware
from rcm.platform.config import Settings, get_settings
from rcm.platform.errors import PlatformError
from rcm.platform.idempotency import IdempotencyStore, RedisIdempotencyStore
from rcm.platform.logging import configure_logging, get_logger

REQUEST_ID_HEADER: Final = "X-Request-ID"
PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    # CLAUDE.md section 12: unresolved specification values are reported, not buried.
    # Logged at startup so "nobody ever decided this" is visible in operations.
    unresolved = settings.unresolved_placeholders()
    if unresolved:
        log.warning(
            "config.placeholders_unresolved",
            count=len(unresolved),
            placeholders=[
                {"setting": name, "tbd": spec.tbd_id, "owner": spec.owner}
                for name, spec in sorted(unresolved.items())
            ],
        )
    log.info("app.started", environment=settings.environment)
    yield
    log.info("app.stopped")


def _default_idempotency_store() -> IdempotencyStore:
    """Redis-backed store, built from settings.

    Imported lazily so that constructing the app does not require a reachable
    Redis at import time -- the connection is established on first use.
    """
    import redis

    settings = get_settings()
    return RedisIdempotencyStore(redis.Redis.from_url(settings.redis_url.get_secret_value()))


def create_app(*, idempotency_store: IdempotencyStore | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="RCM Automation Platform",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment in ("local", "ci") else None,
        redoc_url=None,
    )

    # PD-14 holds for every mutating route because it is applied here, once,
    # rather than per-endpoint. A new route cannot forget it.
    app.add_middleware(
        IdempotencyMiddleware,
        store=idempotency_store or _default_idempotency_store(),
        ttl_seconds=settings.idempotency_key_ttl_seconds,
        max_body_bytes=settings.max_request_body_bytes,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        """Bind a request id to the log context and echo it on the response.

        Accepts an inbound `X-Request-ID` so a trace spans the reverse proxy, but
        generates one when absent -- an unlabelled request cannot be correlated
        with the audit events it produced.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(PlatformError)
    async def platform_error_handler(request: Request, exc: PlatformError) -> JSONResponse:
        problem = exc.to_problem(
            instance=request.url.path,
            request_id=getattr(request.state, "request_id", None),
        )
        log.warning(
            "request.failed",
            code=exc.code,
            status_code=exc.status,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=exc.status, content=problem, media_type=PROBLEM_CONTENT_TYPE
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Anything unnamed becomes a generic 500.

        Deliberately says nothing about what happened: an unexpected exception's
        message is untrusted free text that may contain PHI (PD-13). The
        `request_id` is how an engineer finds the corresponding log entry, which
        carries the exception type and raise site but not its message.
        """
        log.exception("request.unhandled_error", path=request.url.path)
        fallback = PlatformError()
        return JSONResponse(
            status_code=fallback.status,
            content=fallback.to_problem(
                instance=request.url.path,
                request_id=getattr(request.state, "request_id", None),
            ),
            media_type=PROBLEM_CONTENT_TYPE,
        )

    @app.get("/health", tags=["platform"])
    async def health() -> dict[str, str]:
        """Liveness. Deliberately does not touch the database.

        A health check that fails when Postgres is briefly unavailable causes the
        orchestrator to kill an application that would otherwise have recovered.
        Dependency status belongs in a separate readiness probe.
        """
        return {"status": "ok"}

    return app


app = create_app()
