"""Primary, then secondary, behind circuit breakers (M3.7, IR-09).

Each adapter has its own breaker. The router tries the primary unless its
breaker is open, falls back to the secondary on failure or an open circuit,
and raises a named error when neither is available. It never drops a
submission silently: the caller keeps the outbox row and retries (PD-18).
"""

from __future__ import annotations

from datetime import datetime

from rcm.platform.errors import ExternalSystemError, ExternalSystemUnavailableError
from rcm.platform.logging import get_logger
from rcm.submission.adapters import ClearinghouseAdapter, SubmissionReceipt
from rcm.submission.breaker import CircuitBreaker

log = get_logger(__name__)


class ClearinghouseRouter:
    def __init__(
        self,
        *,
        primary: ClearinghouseAdapter,
        secondary: ClearinghouseAdapter,
        failure_threshold: int,
        open_seconds: int,
    ) -> None:
        self._routes = [
            (
                adapter,
                CircuitBreaker(
                    name=adapter.name,
                    failure_threshold=failure_threshold,
                    open_seconds=open_seconds,
                ),
            )
            for adapter in (primary, secondary)
        ]

    def submit_837(
        self, payload: bytes, *, idempotency_key: str, now: datetime
    ) -> SubmissionReceipt:
        for adapter, breaker in self._routes:
            if not breaker.allows(now):
                log.warning("clearinghouse.circuit_open", adapter=adapter.name)
                continue
            try:
                receipt = adapter.submit_837(payload, idempotency_key=idempotency_key)
            except ExternalSystemError:
                breaker.record_failure(now)
                log.warning("clearinghouse.failed", adapter=adapter.name)
                continue
            breaker.record_success()
            return receipt
        raise ExternalSystemUnavailableError(operation="submit_837")
