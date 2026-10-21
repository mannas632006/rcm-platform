"""Retries never duplicate (PD-14, M0.8, FR-API-05).

CLAUDE.md M0.8's acceptance is one line -- *"same key twice -> one effect"* -- and
section 7.1 widens it to "every mutating path and every ingestion path: re-run
changes nothing."

The tests count side effects rather than inspecting the store. A handler here
increments a counter; if the counter moves twice for one key, the control has
failed, whatever the middleware's internal state says. That framing matters
because the thing being prevented is a duplicate *claim submission*, which is a
duplicate bill, not a duplicate cache entry.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcm.platform.config import Settings
from rcm.platform.errors import PlatformError
from rcm.platform.idempotency import (
    IDEMPOTENCY_HEADER,
    REPLAYED_HEADER,
    IdempotencyMiddleware,
    IdempotencyStore,
    fingerprint,
    record_key,
)

pytestmark = pytest.mark.security


class FakeRedis:
    """Enough of the async Redis surface for `IdempotencyStore`.

    Honours `nx` and expiry, because those two are the entire correctness
    argument: `SET NX` is what makes the reservation atomic, and the TTL is what
    stops an abandoned reservation blocking a legitimate retry forever. A fake
    that ignored either would let a broken implementation pass.
    """

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float | None]] = {}
        self.set_calls = 0

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        self.set_calls += 1
        existing = await self.get(key)
        if nx and existing is not None:
            return None
        self.data[key] = (value, time.monotonic() + ex if ex else None)
        return True

    async def get(self, key: str) -> str | None:
        entry = self.data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del self.data[key]
            return None
        return value

    async def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="ci", idempotency_key_ttl_seconds=3600)


@pytest.fixture
def effects() -> dict[str, int]:
    return {"count": 0}


@pytest.fixture
def client(settings: Settings, effects: dict[str, int]) -> TestClient:
    app = FastAPI()
    store = IdempotencyStore(FakeRedis(), ttl_seconds=settings.idempotency_key_ttl_seconds)  # type: ignore[arg-type]
    app.add_middleware(IdempotencyMiddleware, store=store, settings=settings)

    @app.exception_handler(PlatformError)
    async def _problem(_request: Any, exc: PlatformError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content=exc.to_problem())

    @app.post("/claims")
    async def submit(payload: dict[str, Any]) -> dict[str, Any]:
        effects["count"] += 1
        return {"submitted": effects["count"], "claim": payload.get("claim")}

    @app.post("/boom")
    async def boom() -> dict[str, str]:
        effects["count"] += 1
        raise RuntimeError("handler exploded")

    @app.post("/refuse")
    async def refuse() -> Any:
        from fastapi.responses import JSONResponse

        effects["count"] += 1
        return JSONResponse(status_code=422, content={"detail": "no"})

    @app.get("/claims")
    async def read() -> dict[str, str]:
        return {"ok": "true"}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.invariant
def test_same_key_twice_produces_one_effect(client: TestClient, effects: dict[str, int]) -> None:
    """The acceptance condition for M0.8."""
    key = str(uuid.uuid4())
    body = {"claim": "CLM-1"}

    first = client.post("/claims", json=body, headers={IDEMPOTENCY_HEADER: key})
    second = client.post("/claims", json=body, headers={IDEMPOTENCY_HEADER: key})

    assert first.status_code == 200
    assert second.status_code == 200
    assert effects["count"] == 1, "the handler ran twice; PD-14 is not enforced"
    assert first.json() == second.json()
    assert second.headers.get(REPLAYED_HEADER) == "true"
    assert first.headers.get(REPLAYED_HEADER) is None


def test_different_keys_are_separate_effects(client: TestClient, effects: dict[str, int]) -> None:
    """Idempotency must not become accidental deduplication of distinct work."""
    body = {"claim": "CLM-1"}
    client.post("/claims", json=body, headers={IDEMPOTENCY_HEADER: str(uuid.uuid4())})
    client.post("/claims", json=body, headers={IDEMPOTENCY_HEADER: str(uuid.uuid4())})

    assert effects["count"] == 2


def test_missing_key_on_a_mutating_route_is_refused(
    client: TestClient, effects: dict[str, int]
) -> None:
    response = client.post("/claims", json={"claim": "CLM-1"})

    assert response.status_code == 400
    assert response.json()["code"] == "idempotency-key-required"
    assert effects["count"] == 0, "the handler ran despite the request being refused"


def test_reused_key_with_a_different_body_is_a_conflict(
    client: TestClient, effects: dict[str, int]
) -> None:
    """Replaying the stored answer would answer a question nobody asked."""
    key = str(uuid.uuid4())
    client.post("/claims", json={"claim": "CLM-1"}, headers={IDEMPOTENCY_HEADER: key})
    response = client.post("/claims", json={"claim": "CLM-2"}, headers={IDEMPOTENCY_HEADER: key})

    assert response.status_code == 409
    assert response.json()["code"] == "idempotency-conflict"
    assert effects["count"] == 1


def test_failed_request_leaves_the_key_retryable(
    client: TestClient, effects: dict[str, int]
) -> None:
    """A 500 is not made permanent.

    Freezing a transient failure would turn one bad minute into a key the caller
    can never use again -- and the caller cannot pick a new one, because the
    whole point is that it does not know whether the first attempt landed.
    """
    key = str(uuid.uuid4())
    first = client.post("/boom", headers={IDEMPOTENCY_HEADER: key})
    assert first.status_code == 500

    second = client.post("/boom", headers={IDEMPOTENCY_HEADER: key})
    assert second.status_code == 500
    assert effects["count"] == 2, "the reservation was not released after failure"


def test_client_error_is_not_replayed(client: TestClient, effects: dict[str, int]) -> None:
    """A 4xx is the caller's to fix; the corrected retry must reach the handler."""
    key = str(uuid.uuid4())
    client.post("/refuse", headers={IDEMPOTENCY_HEADER: key})
    client.post("/refuse", headers={IDEMPOTENCY_HEADER: key})

    assert effects["count"] == 2


def test_read_requests_need_no_key(client: TestClient) -> None:
    assert client.get("/claims").status_code == 200


def test_keys_are_namespaced_per_principal() -> None:
    """Two tenants choosing the same key must not collide (PD-20 adjacent)."""
    shared = "the-same-key"
    assert record_key(principal_subject="user-a", idempotency_key=shared) != record_key(
        principal_subject="user-b", idempotency_key=shared
    )


def test_redis_key_contains_no_identifier_in_clear() -> None:
    """Redis keys surface in SCAN output and slow logs, outside the PHI boundary."""
    key = record_key(principal_subject="dr.maria.gonzalez@clinic.example", idempotency_key="k")

    assert "maria" not in key.lower()
    assert "clinic.example" not in key


def test_fingerprint_separates_method_and_path() -> None:
    """One key reused across two operations is a bug, not a replay."""
    body = b'{"claim": "CLM-1"}'
    assert fingerprint("POST", "/claims", body) != fingerprint("DELETE", "/claims", body)
    assert fingerprint("POST", "/claims", body) != fingerprint("POST", "/appeals", body)


async def _reserve(store: IdempotencyStore, key: str, fp: str) -> Any:
    return await store.reserve(key, fp)


def test_concurrent_duplicate_is_refused_not_executed_twice() -> None:
    """The loser of a race is told the request is in flight, and does not run.

    GET-then-SET would leave a window where both callers see nothing and both
    proceed. The reservation is a single `SET NX` for this reason.
    """
    import asyncio

    from rcm.platform.errors import RequestInProgressError

    store = IdempotencyStore(FakeRedis(), ttl_seconds=3600)  # type: ignore[arg-type]
    key = record_key(principal_subject="u", idempotency_key="k")
    fp = fingerprint("POST", "/claims", b"{}")

    async def scenario() -> None:
        assert await store.reserve(key, fp) is None  # first caller claims it
        with pytest.raises(RequestInProgressError):
            await store.reserve(key, fp)  # second caller is refused

    asyncio.run(scenario())


def test_stored_response_round_trips() -> None:
    from rcm.platform.idempotency import StoredResponse

    original = StoredResponse(
        status_code=201, body=json.dumps({"id": 1}).encode(), media_type="application/json"
    )
    assert StoredResponse.from_json(original.to_json()) == original
