"""PD-14 / FR-API-01 / EC-41: retries never duplicate.

The side effect under test is a counter. In the real system it is a claim reaching
a payer, which is why EC-41 is High priority: a duplicate submission has to be
withdrawn, and withdrawal is manual.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from rcm.api.idempotency_middleware import IDEMPOTENCY_HEADER, REPLAYED_HEADER
from rcm.api.main import create_app
from rcm.platform.idempotency import (
    IdempotencyRecord,
    InMemoryIdempotencyStore,
    RecordState,
    fingerprint,
)

pytestmark = pytest.mark.security


class Submission(BaseModel):
    claim_id: str


class Effects:
    """Stands in for anything that must happen at most once."""

    def __init__(self) -> None:
        self.submitted: list[str] = []


@pytest.fixture
def effects() -> Effects:
    return Effects()


@pytest.fixture
def store() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def client(effects: Effects, store: InMemoryIdempotencyStore) -> Iterator[TestClient]:
    app: FastAPI = create_app(idempotency_store=store)

    @app.post("/_test/submit")
    async def submit(payload: Submission) -> dict[str, object]:
        effects.submitted.append(payload.claim_id)
        return {"claim_id": payload.claim_id, "attempt": len(effects.submitted)}

    @app.post("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("downstream exploded")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# The property PD-14 claims
# ---------------------------------------------------------------------------


def test_same_key_twice_has_one_effect(client: TestClient, effects: Effects) -> None:
    payload = {"claim_id": "clm-001"}
    headers = {IDEMPOTENCY_HEADER: "key-abc"}

    first = client.post("/_test/submit", json=payload, headers=headers)
    second = client.post("/_test/submit", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert effects.submitted == ["clm-001"], "the side effect ran more than once"
    # The replay is the *stored* response, not a fresh one.
    assert second.json() == first.json()
    assert second.headers.get(REPLAYED_HEADER) == "true"


def test_ten_retries_still_one_effect(client: TestClient, effects: Effects) -> None:
    """A client retrying aggressively is the realistic case, not a single retry."""
    payload = {"claim_id": "clm-002"}
    headers = {IDEMPOTENCY_HEADER: "key-retry"}
    for _ in range(10):
        assert client.post("/_test/submit", json=payload, headers=headers).status_code == 200
    assert effects.submitted == ["clm-002"]


def test_distinct_keys_are_distinct_effects(client: TestClient, effects: Effects) -> None:
    """Idempotency must not collapse genuinely different requests."""
    client.post("/_test/submit", json={"claim_id": "a"}, headers={IDEMPOTENCY_HEADER: "k1"})
    client.post("/_test/submit", json={"claim_id": "b"}, headers={IDEMPOTENCY_HEADER: "k2"})
    assert effects.submitted == ["a", "b"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_mutating_request_without_a_key_is_refused(client: TestClient, effects: Effects) -> None:
    response = client.post("/_test/submit", json={"claim_id": "clm-003"})

    assert response.status_code == 400
    assert response.json()["code"] == "idempotency-key-required"
    assert effects.submitted == [], "the effect ran despite the request being refused"


def test_key_reuse_with_a_different_body_conflicts(client: TestClient, effects: Effects) -> None:
    """The dangerous case: replaying here would report success for work never done."""
    headers = {IDEMPOTENCY_HEADER: "key-shared"}
    client.post("/_test/submit", json={"claim_id": "first"}, headers=headers)
    response = client.post("/_test/submit", json={"claim_id": "second"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "idempotency-conflict"
    assert effects.submitted == ["first"]


def test_concurrent_duplicate_is_told_to_wait(
    client: TestClient, store: InMemoryIdempotencyStore
) -> None:
    """An identical request still in flight gets 409, not a false success.

    Simulated by planting the IN_PROGRESS record the first attempt would have
    written, which is exactly the state a second worker observes.
    """
    body = b'{"claim_id":"clm-004"}'
    store.claim(
        "key-inflight",
        IdempotencyRecord(
            state=RecordState.IN_PROGRESS,
            request_fingerprint=fingerprint("POST", "/_test/submit", body),
        ),
        60,
    )

    response = client.post(
        "/_test/submit",
        content=body,
        headers={IDEMPOTENCY_HEADER: "key-inflight", "content-type": "application/json"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "request-in-progress"


def test_get_requests_need_no_key(client: TestClient) -> None:
    """Reads change nothing, so requiring a key would be friction with no benefit."""
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def test_a_failed_attempt_frees_its_key(
    client: TestClient, store: InMemoryIdempotencyStore
) -> None:
    """A crash must not pin the client to failure for the whole TTL.

    If the key stayed claimed, the legitimate retry would receive
    `request-in-progress` for a request that is not in progress and never will be.
    """
    response = client.post("/_test/boom", headers={IDEMPOTENCY_HEADER: "key-crash"})
    assert response.status_code == 500
    assert store.get("key-crash") is None, "the key was not released after failure"


def test_error_responses_are_not_replayed(client: TestClient, effects: Effects) -> None:
    """Only success is replayable.

    Recording a failure would make a transient error permanent for the key's
    lifetime -- the client could never get past it.
    """
    headers = {IDEMPOTENCY_HEADER: "key-4xx"}
    first = client.post("/_test/submit", content=b"not json", headers=headers)
    assert first.status_code == 422

    second = client.post("/_test/submit", json={"claim_id": "clm-005"}, headers=headers)
    assert second.status_code == 200
    assert effects.submitted == ["clm-005"]
