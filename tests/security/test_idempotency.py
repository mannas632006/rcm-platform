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
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

# Imported at module level, not inside the fixtures that use them: this module
# has `from __future__ import annotations`, so FastAPI resolves a handler's
# annotations against module globals. A name imported inside a fixture is not
# there, and FastAPI then treats the unresolved parameter as a request body
# field -- the route answers 422 instead of running.
from rcm.api.dependencies import current_principal
from rcm.platform.auth import Principal, Role, issue_dev_token
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


# ---------------------------------------------------------------------------
# Cross-tenant isolation of the replay cache
# ---------------------------------------------------------------------------
#
# These exercise the middleware through a real app with real tokens. The
# `record_key` unit test above cannot catch what they catch: it calls the
# namespacing helper directly, so it passed while the middleware was feeding
# that helper the same constant for every caller.


@pytest.fixture
def auth_settings() -> Settings:
    return Settings(
        environment="ci",
        dev_jwt_secret=SecretStr("ci-only-test-signing-key-32bytes"),
        idempotency_key_ttl_seconds=3600,
    )


@pytest.fixture
def tenant_client(auth_settings: Settings) -> TestClient:
    """An app whose handler echoes which principal it served."""
    app = FastAPI()
    store = IdempotencyStore(FakeRedis(), ttl_seconds=3600)  # type: ignore[arg-type]
    app.add_middleware(IdempotencyMiddleware, store=store, settings=auth_settings)

    @app.exception_handler(PlatformError)
    async def _problem(_request: Any, exc: PlatformError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content=exc.to_problem())

    @app.post("/claims")
    async def submit(
        payload: dict[str, Any],
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> dict[str, Any]:
        return {"served_to": principal.subject, "claim": payload.get("claim")}

    return TestClient(app, raise_server_exceptions=False)


def _token(settings: Settings, subject: str) -> str:
    return issue_dev_token(
        subject=subject,
        roles=[Role.AR_SPECIALIST],
        client_ids=[uuid.uuid4()],
        settings=settings,
    )


@pytest.mark.invariant
def test_one_tenant_cannot_replay_another_tenants_response(
    tenant_client: TestClient, auth_settings: Settings
) -> None:
    """The regression test for a cross-tenant disclosure via the replay cache.

    Two tenants independently choosing the same `Idempotency-Key` -- which is
    likely, because keys are often derived from a batch date or a run number --
    must not see each other's responses. Before the middleware authenticated
    the caller itself, the second tenant received the first tenant's stored
    response body verbatim.
    """
    shared_key = "batch-2026-10-21-001"
    body = {"claim": "CLM-A-CONFIDENTIAL"}

    alice = tenant_client.post(
        "/claims",
        json=body,
        headers={
            IDEMPOTENCY_HEADER: shared_key,
            "Authorization": f"Bearer {_token(auth_settings, 'alice@tenant-a')}",
        },
    )
    bob = tenant_client.post(
        "/claims",
        json=body,
        headers={
            IDEMPOTENCY_HEADER: shared_key,
            "Authorization": f"Bearer {_token(auth_settings, 'bob@tenant-b')}",
        },
    )

    assert alice.json()["served_to"] == "alice@tenant-a"
    assert bob.json()["served_to"] == "bob@tenant-b", (
        "tenant B was served tenant A's stored response: the replay cache is "
        "not namespaced per principal"
    )
    assert bob.headers.get(REPLAYED_HEADER) is None, "tenant B's request was answered from cache"


def test_the_same_principal_still_replays(
    tenant_client: TestClient, auth_settings: Settings
) -> None:
    """Namespacing must not break idempotency for the caller it is meant to serve."""
    token = _token(auth_settings, "alice@tenant-a")
    headers = {IDEMPOTENCY_HEADER: "same-key", "Authorization": f"Bearer {token}"}

    first = tenant_client.post("/claims", json={"claim": "CLM-1"}, headers=headers)
    second = tenant_client.post("/claims", json={"claim": "CLM-1"}, headers=headers)

    assert first.json() == second.json()
    assert second.headers.get(REPLAYED_HEADER) == "true"


def test_a_conflict_cannot_reveal_another_tenants_key(
    tenant_client: TestClient, auth_settings: Settings
) -> None:
    """Reusing a key with a different body is a 409 only within one principal.

    Shared namespacing would turn that 409 into an oracle: a caller could
    discover which keys another tenant had already used.
    """
    key = "batch-2026-10-21-002"
    alice_headers = {
        IDEMPOTENCY_HEADER: key,
        "Authorization": f"Bearer {_token(auth_settings, 'alice@tenant-a')}",
    }
    bob_headers = {
        IDEMPOTENCY_HEADER: key,
        "Authorization": f"Bearer {_token(auth_settings, 'bob@tenant-b')}",
    }

    tenant_client.post("/claims", json={"claim": "CLM-A"}, headers=alice_headers)

    def post(body: str, headers: dict[str, str]) -> int:
        return tenant_client.post("/claims", json={"claim": body}, headers=headers).status_code

    assert post("CLM-A", alice_headers) == 200, "Alice replays her own call"
    assert post("CLM-OTHER", alice_headers) == 409, "a different body under her key conflicts"
    assert post("CLM-B", bob_headers) == 200, "Bob's identical key is a separate namespace"
