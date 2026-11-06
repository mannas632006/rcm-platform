"""Claim Detail's pure rules: the filing clock (UR-07) and member-id masking (S-6).

PD-18: each says what it knows. A claim that was never filed must not read
as filed, and a missing member id must not look like a masked one.
"""

from __future__ import annotations

from datetime import date

import pytest

from rcm.domain.claim_state import ClaimState
from rcm.work.claim_detail import (
    FILED_STATES,
    NO_FILING_STATES,
    FilingStatus,
    filing,
    mask,
)
from rcm.work.priority import FILING_CLOCK_STATES

TODAY = date(2026, 11, 6)


def test_every_state_has_exactly_one_filing_meaning() -> None:
    """A new state must be placed deliberately, not fall into a default."""
    groups = (FILING_CLOCK_STATES, FILED_STATES, NO_FILING_STATES)
    for state in ClaimState:
        assert sum(state in g for g in groups) == 1, state


@pytest.mark.parametrize(
    "state", [ClaimState.AUTH_DENIED, ClaimState.CANCELLED, ClaimState.REPLACED]
)
def test_a_claim_never_filed_does_not_read_as_filed(state: ClaimState) -> None:
    clock = filing(state, term=90, service_date=TODAY, today=TODAY)
    assert clock.status is FilingStatus.NOT_APPLICABLE
    assert clock.days_left is None


def test_a_claim_the_payer_took_is_filed() -> None:
    clock = filing(ClaimState.DENIED, term=90, service_date=TODAY, today=TODAY)
    assert clock.status is FilingStatus.FILED


def test_the_clock_counts_down_from_the_date_of_service() -> None:
    clock = filing(ClaimState.SUBMITTED, term=90, service_date=date(2026, 10, 1), today=TODAY)
    assert (clock.status, clock.days_left) == (FilingStatus.RUNNING, 54)


@pytest.mark.parametrize(("term", "service"), [(None, TODAY), (90, None)])
def test_an_unknown_term_or_date_is_unknown_not_guessed(
    term: int | None, service: date | None
) -> None:
    clock = filing(ClaimState.SUBMITTED, term=term, service_date=service, today=TODAY)
    assert (clock.status, clock.days_left) == (FilingStatus.UNKNOWN, None)


def test_mask_keeps_the_last_four() -> None:
    assert mask("W8842119301") == "••••9301"


def test_a_short_id_is_hidden_entirely() -> None:
    assert mask("1234") == "••••"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_missing_member_id_is_none_not_a_mask(blank: str) -> None:
    assert mask(blank) is None
