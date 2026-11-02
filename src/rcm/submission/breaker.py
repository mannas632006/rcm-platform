"""A circuit breaker for one clearinghouse (M3.7, IR-09).

    IR-09 (MUST): A circuit breaker MUST trigger failover. The February 2024
    Change Healthcare outage halted claim flow industry-wide for weeks.

Closed: calls go through. After `failure_threshold` *consecutive* failures it
opens, and calls are refused without being attempted -- so a dead
clearinghouse costs one fast refusal per claim rather than a timeout each.
After `open_seconds` it is half-open: the next call is a probe. A probe that
succeeds closes it; one that fails reopens it for a full period.

The clock is a parameter, as everywhere in this codebase: state is a
function of `now`, so every transition is testable without sleeping.

State is per process. With several workers each keeps its own breaker,
which only means each discovers an outage independently -- none ever sends
to a clearinghouse its own breaker has opened.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Consecutive-failure breaker with a timed half-open probe."""

    def __init__(self, *, name: str, failure_threshold: int, open_seconds: int) -> None:
        if failure_threshold < 1 or open_seconds < 1:
            raise ValueError("a breaker needs a threshold and an open period of at least 1")
        self.name = name
        self._threshold = failure_threshold
        self._open_for = timedelta(seconds=open_seconds)
        self._failures = 0
        self._opened_at: datetime | None = None

    def state(self, now: datetime) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if now - self._opened_at >= self._open_for:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allows(self, now: datetime) -> bool:
        return self.state(now) is not BreakerState.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self, now: datetime) -> None:
        # The count is only reset by a success, so a failed half-open probe
        # is already past the threshold and reopens for a full period.
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = now
