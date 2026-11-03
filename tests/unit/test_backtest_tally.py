"""How a backtest counts (M4.4, BR-M7-04, ADR-0009).

FP rate = false positives / flagged: the share of claims a rule would have
stopped that the payer in fact paid. TBD-45 is measured on this number.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rcm.decision.rules.backtest import Tally, tally
from rcm.decision.rules.evaluate import Outcome

F = Outcome.FLAGGED
I = Outcome.INDETERMINATE  # noqa: E741


def test_each_kind_of_result_is_counted_once() -> None:
    result = tally(
        [
            (F, True),  # true positive
            (F, False),  # false positive
            (None, True),  # missed denial
            (None, False),  # correctly passed
            (I, True),  # could not be read
            (F, None),  # no outcome yet
        ]
    )
    assert result == Tally(
        evaluated=5,
        flagged=2,
        true_positives=1,
        false_positives=1,
        missed_denials=1,
        indeterminate=1,
        without_outcome=1,
    )


def test_the_fp_rate_is_false_positives_over_flagged() -> None:
    result = tally([(F, True)] * 9 + [(F, False)])
    assert result.fp_rate == Decimal("0.1000")


def test_a_rule_that_flags_nothing_has_no_fp_rate() -> None:
    """Not 0%: a rule never tested against a claim it would stop has not
    shown it is accurate, and must not pass a threshold as if it had."""
    assert tally([(None, True), (None, False)]).fp_rate is None


@pytest.mark.parametrize(
    "results",
    [
        [],
        [(F, None), (None, None)],
    ],
)
def test_no_claim_with_an_outcome_evaluates_nothing(
    results: list[tuple[Outcome | None, bool | None]],
) -> None:
    result = tally(results)
    assert result.evaluated == 0
    assert result.fp_rate is None


def test_an_indeterminate_result_is_neither_a_hit_nor_a_miss() -> None:
    result = tally([(I, True), (I, False)])
    assert (result.flagged, result.true_positives, result.missed_denials) == (0, 0, 0)
    assert result.indeterminate == 2
