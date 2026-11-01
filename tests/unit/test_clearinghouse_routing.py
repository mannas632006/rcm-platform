"""The circuit breaker and the clearinghouse router (M3.7, IR-07..IR-09).

    IR-08 (MUST): All clearinghouse interaction goes through the adapter
    interface.
    IR-09 (MUST): A circuit breaker MUST trigger failover.

No database and no sleeping: every call takes the clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rcm.platform.errors import (
    ExternalSystemError,
    ExternalSystemOutcomeUnknownError,
    ExternalSystemRejectedError,
    ExternalSystemUnavailableError,
)
from rcm.submission.adapters import MockAdapter
from rcm.submission.breaker import BreakerState, CircuitBreaker
from rcm.submission.router import ClearinghouseRouter

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
PAYLOAD = b"ISA*00*~"


def breaker() -> CircuitBreaker:
    return CircuitBreaker(name="primary", failure_threshold=3, open_seconds=60)


# ---------------------------------------------------------------------------
# The breaker
# ---------------------------------------------------------------------------


def test_a_closed_breaker_lets_calls_through() -> None:
    assert breaker().allows(T0)


def test_it_opens_after_the_threshold_of_consecutive_failures() -> None:
    b = breaker()
    for _ in range(3):
        b.record_failure(T0)
    assert b.state(T0) is BreakerState.OPEN
    assert not b.allows(T0 + timedelta(seconds=59))


def test_a_success_resets_the_count() -> None:
    """Failures must be consecutive; intermittent noise does not trip it."""
    b = breaker()
    b.record_failure(T0)
    b.record_failure(T0)
    b.record_success()
    b.record_failure(T0)
    assert b.state(T0) is BreakerState.CLOSED


def test_after_the_open_period_one_probe_is_allowed() -> None:
    b = breaker()
    for _ in range(3):
        b.record_failure(T0)
    later = T0 + timedelta(seconds=60)
    assert b.state(later) is BreakerState.HALF_OPEN
    assert b.allows(later)


def test_a_successful_probe_closes_it() -> None:
    b = breaker()
    for _ in range(3):
        b.record_failure(T0)
    b.record_success()
    assert b.state(T0 + timedelta(seconds=61)) is BreakerState.CLOSED


def test_a_failed_probe_reopens_it_for_a_full_period() -> None:
    b = breaker()
    for _ in range(3):
        b.record_failure(T0)
    probe_time = T0 + timedelta(seconds=60)
    b.record_failure(probe_time)
    assert not b.allows(probe_time + timedelta(seconds=59))
    assert b.allows(probe_time + timedelta(seconds=60))


@pytest.mark.parametrize(("threshold", "seconds"), [(0, 60), (3, 0)])
def test_nonsense_settings_are_refused(threshold: int, seconds: int) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(name="x", failure_threshold=threshold, open_seconds=seconds)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def test_the_mock_adapter_does_not_transmit_a_key_twice() -> None:
    """IR-07 / EC-41: a retry with the same key is the same submission."""
    adapter = MockAdapter("primary")
    first = adapter.submit_837(PAYLOAD, idempotency_key="k1")
    second = adapter.submit_837(PAYLOAD, idempotency_key="k1")
    assert first == second
    assert adapter.transmissions == 1


def test_a_failing_mock_raises_a_named_error() -> None:
    adapter = MockAdapter("primary", failing=True)
    with pytest.raises(ExternalSystemError):
        adapter.submit_837(PAYLOAD, idempotency_key="k1")
    assert adapter.transmissions == 0
    assert adapter.health_check() is False


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


def router(primary: MockAdapter, secondary: MockAdapter) -> ClearinghouseRouter:
    return ClearinghouseRouter(
        primary=primary,
        secondary=secondary,
        failure_threshold=3,
        open_seconds=60,
    )


def test_a_healthy_primary_is_used() -> None:
    primary, secondary = MockAdapter("primary"), MockAdapter("secondary")
    receipt = router(primary, secondary).submit_837(PAYLOAD, idempotency_key="k", now=T0)
    assert receipt.adapter == "primary"
    assert (primary.transmissions, secondary.transmissions) == (1, 0)


@pytest.mark.invariant
def test_primary_failure_fails_over_to_secondary() -> None:
    """IR-09: the February 2024 outage is the reason this exists."""
    primary, secondary = MockAdapter("primary", failing=True), MockAdapter("secondary")
    receipt = router(primary, secondary).submit_837(PAYLOAD, idempotency_key="k", now=T0)
    assert receipt.adapter == "secondary"
    assert secondary.transmissions == 1


def test_an_open_circuit_skips_the_primary_entirely() -> None:
    """Once open, the primary is not even tried -- that is what stops a dead
    clearinghouse costing a timeout on every claim."""
    primary, secondary = MockAdapter("primary", failing=True), MockAdapter("secondary")
    r = router(primary, secondary)
    for i in range(3):
        r.submit_837(PAYLOAD, idempotency_key=f"k{i}", now=T0)
    calls_before = primary.attempts
    r.submit_837(PAYLOAD, idempotency_key="k-next", now=T0 + timedelta(seconds=1))
    assert primary.attempts == calls_before


@pytest.mark.invariant
def test_both_failing_raises_a_named_error() -> None:
    """IR-09 / PD-18: no route is a named, loud failure -- never a silent drop."""
    primary = MockAdapter("primary", failing=True)
    secondary = MockAdapter("secondary", failing=True)
    with pytest.raises(ExternalSystemUnavailableError):
        router(primary, secondary).submit_837(PAYLOAD, idempotency_key="k", now=T0)


def test_the_primary_comes_back_after_its_probe_succeeds() -> None:
    primary, secondary = MockAdapter("primary", failing=True), MockAdapter("secondary")
    r = router(primary, secondary)
    for i in range(3):
        r.submit_837(PAYLOAD, idempotency_key=f"k{i}", now=T0)
    primary.failing = False
    receipt = r.submit_837(PAYLOAD, idempotency_key="k-probe", now=T0 + timedelta(seconds=61))
    assert receipt.adapter == "primary"


# ---------------------------------------------------------------------------
# Review findings: what a failure proves decides what happens next
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_lost_response_never_fails_over() -> None:
    """The primary accepted the file and the answer was lost. The secondary
    has never seen the key, so sending there would submit the claim twice."""
    primary = MockAdapter("primary", loses_response=True)
    secondary = MockAdapter("secondary")
    with pytest.raises(ExternalSystemOutcomeUnknownError):
        router(primary, secondary).submit_837(PAYLOAD, idempotency_key="k", now=T0)
    assert primary.transmissions == 1
    assert secondary.attempts == 0


def test_an_unclassified_adapter_failure_is_outcome_unknown() -> None:
    """A socket error says nothing about whether the file arrived."""
    primary, secondary = MockAdapter("primary", crashes=True), MockAdapter("secondary")
    with pytest.raises(ExternalSystemOutcomeUnknownError):
        router(primary, secondary).submit_837(PAYLOAD, idempotency_key="k", now=T0)
    assert secondary.attempts == 0


def test_a_rejection_is_not_an_outage() -> None:
    """Bad files do not take a healthy clearinghouse out of service: no
    failover, no strike against the circuit."""
    primary, secondary = MockAdapter("primary", rejecting=True), MockAdapter("secondary")
    r = router(primary, secondary)
    for i in range(5):
        with pytest.raises(ExternalSystemRejectedError):
            r.submit_837(PAYLOAD, idempotency_key=f"bad{i}", now=T0)
    assert secondary.attempts == 0
    primary.rejecting = False
    assert r.submit_837(PAYLOAD, idempotency_key="good", now=T0).adapter == "primary"
