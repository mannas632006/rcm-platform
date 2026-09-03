"""Idempotency record store (PD-14, FR-API-01, EC-41).

PD-14: *"Every mutating endpoint accepts `Idempotency-Key`. Retries never
duplicate."* In a system that submits claims and posts money, a duplicated retry
is not a cosmetic bug -- EC-41 ("duplicate from retry") is a High-priority edge
case precisely because a duplicate claim reaches the payer and has to be
withdrawn.

The store answers one question: has this exact request already been handled?
"Exact" includes the request body, so that a client which reuses a key for a
different payload gets a loud conflict rather than someone else's response.

Three outcomes, and the distinction between the last two matters:

- **NEW** -- nothing recorded; proceed and record the result.
- **IN_PROGRESS** -- an identical request is running right now. The caller retried
  before the first attempt finished. Returning "success" would be a lie and
  returning "not found" invites a third attempt, so this is a 409 and the client
  waits.
- **COMPLETED** -- replay the stored response verbatim. No side effect repeats.

A different body under the same key is a *conflict*, never a replay.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

#: Methods that change state and therefore require a key.
MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RecordState(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """What is remembered about one keyed request."""

    state: RecordState
    request_fingerprint: str
    status_code: int | None = None
    body: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "state": str(self.state),
                "request_fingerprint": self.request_fingerprint,
                "status_code": self.status_code,
                "body": self.body,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> IdempotencyRecord:
        data: dict[str, Any] = json.loads(raw)
        return cls(
            state=RecordState(data["state"]),
            request_fingerprint=data["request_fingerprint"],
            status_code=data.get("status_code"),
            body=data.get("body"),
        )


def fingerprint(method: str, path: str, body: bytes) -> str:
    """Identify a request by what it asks for, not only by its key.

    Without the body in the hash, a client that reuses a key for a different
    payload would silently receive the earlier response -- the second request
    would appear to succeed while never having been performed.
    """
    digest = hashlib.sha256()
    digest.update(method.upper().encode())
    digest.update(b"\x00")
    digest.update(path.encode())
    digest.update(b"\x00")
    digest.update(body)
    return digest.hexdigest()


class IdempotencyStore(Protocol):
    """Storage for idempotency records.

    Implemented over Redis in production. The `claim` operation must be atomic:
    two concurrent retries of the same request must not both be told they are
    first, or the side effect happens twice -- which is the whole failure mode.
    """

    def claim(self, key: str, record: IdempotencyRecord, ttl_seconds: int) -> bool:
        """Atomically record `key` if absent. True when the caller now owns it."""
        ...

    def get(self, key: str) -> IdempotencyRecord | None: ...

    def complete(self, key: str, record: IdempotencyRecord, ttl_seconds: int) -> None:
        """Overwrite an IN_PROGRESS record with its final response."""
        ...

    def release(self, key: str) -> None:
        """Drop an IN_PROGRESS record whose request failed unexpectedly.

        Without this, a request that crashes mid-flight would poison its key for
        the whole TTL and the client could never legitimately retry.
        """
        ...


class RedisIdempotencyStore:
    """Redis-backed store. `claim` uses SET NX, which is atomic across workers."""

    def __init__(self, client: Any) -> None:
        self._redis = client

    @staticmethod
    def _namespaced(key: str) -> str:
        return f"idempotency:{key}"

    def claim(self, key: str, record: IdempotencyRecord, ttl_seconds: int) -> bool:
        return bool(
            self._redis.set(self._namespaced(key), record.to_json(), nx=True, ex=ttl_seconds)
        )

    def get(self, key: str) -> IdempotencyRecord | None:
        raw = self._redis.get(self._namespaced(key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return IdempotencyRecord.from_json(raw)

    def complete(self, key: str, record: IdempotencyRecord, ttl_seconds: int) -> None:
        self._redis.set(self._namespaced(key), record.to_json(), ex=ttl_seconds)

    def release(self, key: str) -> None:
        self._redis.delete(self._namespaced(key))


class InMemoryIdempotencyStore:
    """Single-process store for tests and local development.

    Not for deployed use: it is per-process, so two API workers would each think
    they were first. `claim` is still written as a single check-and-set so the
    semantics match the Redis implementation.
    """

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}

    def claim(self, key: str, record: IdempotencyRecord, ttl_seconds: int) -> bool:
        del ttl_seconds  # no expiry in-process; records live for the test
        if key in self._records:
            return False
        self._records[key] = record
        return True

    def get(self, key: str) -> IdempotencyRecord | None:
        return self._records.get(key)

    def complete(self, key: str, record: IdempotencyRecord, ttl_seconds: int) -> None:
        del ttl_seconds
        self._records[key] = record

    def release(self, key: str) -> None:
        self._records.pop(key, None)
