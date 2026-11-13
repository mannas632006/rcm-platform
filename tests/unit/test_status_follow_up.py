"""What a payer's status answers ask a person to do (ADR-0033, EC-54, EC-56).

`decide` is pure: one claim's answers, oldest first, and a clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rcm.domain.acknowledgement import ClaimStatusAnswer
from rcm.platform.config import get_settings
from rcm.work.status_follow_up import decide

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def answer(
    kind: str, category: str, status: str = "", *, days_ago: int = 0, text: str = ""
) -> ClaimStatusAnswer:
    return ClaimStatusAnswer(
        answer=kind,
        category_code=category,
        status_code=status,
        description=text,
        received_at=NOW - timedelta(days=days_ago),
    )


@pytest.mark.parametrize(
    ("kind", "category"),
    [("PAID", "F1"), ("DENIED", "F2"), ("NO_PAYMENT", "F4"), ("FINALIZED", "F0")],
)
def test_a_decided_claim_with_no_835_is_chased_once_the_wait_is_over(
    kind: str, category: str
) -> None:
    """EC-54: the payer says it decided, the 835 never came."""
    grace = get_settings().status_remittance_grace_days
    assert decide([answer(kind, category, days_ago=grace - 1)], now=NOW) is None
    follow_up = decide([answer(kind, category, days_ago=grace)], now=NOW)
    assert follow_up is not None
    assert follow_up.key == "status-decided-no-835"
    assert category in follow_up.problem
    assert "835" in follow_up.problem


def test_repeated_pends_with_no_reason_go_to_a_person() -> None:
    """EC-56: pended again and again, never saying why."""
    limit = get_settings().status_pending_repeat_limit
    pends = [answer("PENDING", "P1", "20", days_ago=d) for d in range(limit, 0, -1)]
    assert decide(pends[:-1], now=NOW) is None
    follow_up = decide(pends, now=NOW)
    assert follow_up is not None
    assert follow_up.key == "status-pending-no-reason"
    assert f"{limit} times" in follow_up.problem


@pytest.mark.parametrize(
    "reasoned",
    [answer("PENDING", "P2", "187"), answer("PENDING", "P1", "", text="awaiting review")],
    ids=["status-code", "payer-text"],
)
def test_a_pend_that_states_its_reason_is_waited_out(reasoned: ClaimStatusAnswer) -> None:
    limit = get_settings().status_pending_repeat_limit
    pends = [answer("PENDING", "P1", "0", days_ago=d) for d in range(limit, 1, -1)]
    assert decide([*pends, reasoned], now=NOW) is None


def test_only_the_latest_run_of_pends_counts() -> None:
    """A claim answered otherwise in between has not been pending all along."""
    limit = get_settings().status_pending_repeat_limit
    history = [
        *(answer("PENDING", "P1", "20", days_ago=10 + d) for d in range(limit)),
        answer("ACKNOWLEDGED", "A2", "20", days_ago=5),
        answer("PENDING", "P1", "20", days_ago=1),
    ]
    assert decide(history, now=NOW) is None


@pytest.mark.parametrize(
    ("kind", "category", "key"),
    [
        ("INFO_REQUESTED", "R4", "status-info-requested"),
        ("NOT_FOUND", "A4", "status-not-found"),
        ("RETURNED", "A7", "status-returned"),
        ("NO_ANSWER", "E1", "status-no-answer"),
    ],
)
def test_answers_that_need_a_person_now_say_what_to_do(kind: str, category: str, key: str) -> None:
    follow_up = decide([answer(kind, category, "21")], now=NOW)
    assert follow_up is not None
    assert follow_up.key == key
    assert f"{category}:21" in follow_up.problem


def test_an_acknowledgement_needs_nothing() -> None:
    assert decide([answer("ACKNOWLEDGED", "A2", "20")], now=NOW) is None


def test_the_payers_own_words_are_never_put_in_the_item() -> None:
    """PD-13: STC12 is free text and can carry a patient's details."""
    words = "Member Jane Doe DOB 01/02/1980"
    follow_up = decide([answer("INFO_REQUESTED", "R4", "15", text=words)], now=NOW)
    assert follow_up is not None
    assert words not in follow_up.problem
    assert words not in follow_up.detail
