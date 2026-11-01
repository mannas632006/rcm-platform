"""The clearinghouse adapter interface, and the mock (M3.7, IR-08).

    IR-08 (MUST): All clearinghouse interaction goes through the adapter
    interface. No clearinghouse-specific code outside an adapter.

SRS 6.4 also lists `fetch_835` and `submit_270/276/278`; they join the
interface when the eligibility and status modules need them. Only the mock
implements it: a real adapter needs a clearinghouse contract and
credentials, which is a human checkpoint.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from rcm.platform.errors import ExternalSystemError


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    """The clearinghouse's transport acknowledgement of one file."""

    adapter: str
    receipt_id: str
    idempotency_key: str


class ClearinghouseAdapter(Protocol):
    name: str

    def submit_837(self, payload: bytes, *, idempotency_key: str) -> SubmissionReceipt:
        """Transmit one 837. The same key twice is one transmission (IR-07)."""
        ...

    def fetch_responses(self) -> list[bytes]:
        """999, 277CA and 835 files waiting to be collected."""
        ...

    def health_check(self) -> bool: ...


class MockAdapter:
    """An in-memory clearinghouse for synthetic data and tests.

    Honours idempotency keys the way a real clearinghouse's API should: a
    key it has already accepted returns the original receipt and is not
    transmitted again (EC-41).
    """

    def __init__(self, name: str, *, failing: bool = False) -> None:
        self.name = name
        self.failing = failing
        #: Every call, successful or not.
        self.attempts = 0
        self._accepted: dict[str, SubmissionReceipt] = {}
        self.received: list[bytes] = []

    @property
    def transmissions(self) -> int:
        """Files actually transmitted -- a duplicate send counts twice."""
        return len(self.received)

    def submit_837(self, payload: bytes, *, idempotency_key: str) -> SubmissionReceipt:
        self.attempts += 1
        if self.failing:
            raise ExternalSystemError(system=self.name, operation="submit_837")
        if idempotency_key in self._accepted:
            return self._accepted[idempotency_key]
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
        receipt = SubmissionReceipt(
            adapter=self.name, receipt_id=f"{self.name}-{digest}", idempotency_key=idempotency_key
        )
        self._accepted[idempotency_key] = receipt
        self.received.append(payload)
        return receipt

    def fetch_responses(self) -> list[bytes]:
        if self.failing:
            raise ExternalSystemError(system=self.name, operation="fetch_responses")
        return []

    def health_check(self) -> bool:
        return not self.failing
