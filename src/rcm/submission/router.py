"""Primary, then secondary, behind circuit breakers (M3.7, IR-09).

Each adapter has its own breaker. What happens on failure depends on what
the failure *proves*:

- **Definitely not delivered** (`ExternalSystemError`): counts against the
  circuit and fails over. The secondary cannot receive a duplicate of a file
  the primary never accepted.
- **Rejected** (`ExternalSystemRejectedError`): the clearinghouse is up and
  said no to this file. No failover -- it would be refused again -- and no
  strike against the circuit, or a few bad files would take a healthy
  clearinghouse out of service.
- **Outcome unknown** (`ExternalSystemOutcomeUnknownError`, or any exception
  an adapter did not classify): counts against the circuit, and does *not*
  fail over. If the primary did accept the file, the secondary has never
  seen its idempotency key and would accept it again -- one claim, two
  submissions. A person confirms instead (PD-18).

Nothing is dropped: the caller keeps the outbox row and decides.
"""

from __future__ import annotations

from datetime import datetime

from rcm.platform.errors import (
    ExternalSystemError,
    ExternalSystemOutcomeUnknownError,
    ExternalSystemRejectedError,
    ExternalSystemUnavailableError,
)
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
            except ExternalSystemRejectedError:
                # Reachable and answering: that is a healthy clearinghouse.
                breaker.record_success()
                log.warning("clearinghouse.rejected", adapter=adapter.name)
                raise
            except ExternalSystemOutcomeUnknownError:
                breaker.record_failure(now)
                log.error("clearinghouse.outcome_unknown", adapter=adapter.name)
                raise
            except ExternalSystemError as err:
                breaker.record_failure(now)
                log.warning("clearinghouse.not_delivered", adapter=adapter.name, code=err.code)
                continue
            except Exception as err:
                # An adapter that did not classify its failure has not told
                # us the file was not delivered. Treat it as unknown.
                breaker.record_failure(now)
                log.error(
                    "clearinghouse.unclassified_failure",
                    adapter=adapter.name,
                    error_type=type(err).__name__,
                )
                raise ExternalSystemOutcomeUnknownError(
                    system=adapter.name, operation="submit_837"
                ) from err
            breaker.record_success()
            return receipt
        raise ExternalSystemUnavailableError(operation="submit_837")
