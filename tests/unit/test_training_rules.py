"""Which rows may train, and how they are split (M2.5, M2.6).

Both rules exist because breaking them produces a model that scores well and
is wrong, and the wrongness is invisible: every number involved is a real
number computed from real rows.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rcm.analytics.training import (
    DEFAULT_MATURITY_DAYS,
    SplitStrategy,
    is_mature,
    maturity_date,
    split,
)
from rcm.platform.errors import LeakageError

CUTOFF = date(2024, 6, 30)


# ---------------------------------------------------------------------------
# DR-08
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@pytest.mark.parametrize("strategy", ["random", "RANDOM", "Random", "shuffle"])
def test_random_split_is_rejected(strategy: str) -> None:
    """DR-08. Not unsupported -- refused, by name, with the reason.

    A random split puts claims from the same week on both sides of the
    boundary, so the model is scored on payer behaviour it has already seen
    and the number that comes out is higher than the truth.
    """
    with pytest.raises(LeakageError) as raised:
        split(strategy=strategy, cutoff=CUTOFF)

    assert (
        "already seen" in raised.value.context["reason"]
        or "time ordering" in (raised.value.context["reason"])
    )


@pytest.mark.parametrize("strategy", ["stratified", "kfold", "group"])
def test_the_other_leaking_strategies_are_refused_too(strategy: str) -> None:
    """The ones a reasonable person tries next after `random` fails.

    Each is a random split with extra steps, and each is refused with the
    specific reason rather than a generic "invalid value" -- a developer
    told "random is not supported" goes looking for the supported one.
    """
    with pytest.raises(LeakageError) as raised:
        split(strategy=strategy, cutoff=CUTOFF)
    assert raised.value.context["strategy"] == strategy.upper()
    assert raised.value.context["reason"]


def test_a_temporal_split_is_accepted() -> None:
    assert split(strategy="TEMPORAL", cutoff=CUTOFF) is SplitStrategy.TEMPORAL
    assert split(strategy=SplitStrategy.TEMPORAL, cutoff=CUTOFF) is SplitStrategy.TEMPORAL


def test_a_temporal_split_without_a_cutoff_is_refused() -> None:
    """A boundary is the whole content of a temporal split. Defaulting it --
    to today, to the median date, to anything -- would silently pick where
    the test set starts, which is the one decision the caller must make."""
    with pytest.raises(LeakageError):
        split(strategy="TEMPORAL", cutoff=None)


def test_an_unknown_strategy_is_refused_rather_than_ignored() -> None:
    """PD-18. An unrecognised strategy is not a reason to fall through to the
    default one."""
    with pytest.raises(LeakageError):
        split(strategy="whatever-the-notebook-said", cutoff=CUTOFF)


def test_the_enum_offers_no_second_strategy() -> None:
    """The refusals above are the guard for a string arriving from config.

    This is the guard for the API surface itself: if a `RANDOM` member ever
    appears here, someone can select it without going through `split()` at
    all, and every refusal above becomes decorative.
    """
    assert list(SplitStrategy) == [SplitStrategy.TEMPORAL]


# ---------------------------------------------------------------------------
# DR-09
# ---------------------------------------------------------------------------


def test_maturity_is_measured_from_the_date_of_service() -> None:
    """Not from adjudication: a recoupment is timed against the service, not
    against the cheque that paid for it."""
    assert maturity_date(date(2024, 1, 1), maturity_days=180) == date(2024, 6, 29)


def test_the_default_window_is_the_labelled_placeholder() -> None:
    """[TBD-50]. DR-09 recommends 180 days; that is a recommendation in a
    specification, not a measurement of this book of business, and it stays
    a named constant so the sponsor's decision lands in one place."""
    assert DEFAULT_MATURITY_DAYS == 180
    assert maturity_date(date(2024, 1, 1)) == date(2024, 1, 1) + timedelta(days=180)


@pytest.mark.invariant
def test_an_immature_row_is_not_usable_for_final_outcome() -> None:
    """DR-09. A claim paid in March and recouped in September was, in April,
    a paid claim. Anything reading it in April read it correctly and still
    got the wrong answer."""
    dos = date(2024, 1, 1)
    assert is_mature(dos, as_of=date(2024, 3, 1)) is False
    assert is_mature(dos, as_of=date(2024, 6, 28)) is False
    assert is_mature(dos, as_of=date(2024, 6, 29)) is True
    assert is_mature(dos, as_of=date(2025, 1, 1)) is True


def test_a_row_with_no_date_of_service_is_never_mature() -> None:
    """PD-18, fail closed. A row that cannot be aged is exactly the row
    nothing can vouch for, and treating an unknown date as mature admits it
    to the training set."""
    assert is_mature(None, as_of=date(2030, 1, 1)) is False


def test_a_negative_maturity_window_is_refused() -> None:
    """It would mature rows before they happened, which is not a shorter
    window -- it is the gate inverted."""
    with pytest.raises(LeakageError):
        maturity_date(date(2024, 1, 1), maturity_days=-1)


def test_a_zero_window_is_allowed_and_means_no_gate() -> None:
    """Deliberately permitted: a sponsor may decide the window is zero, and
    that is a decision rather than an error. It is distinguishable from the
    default, which is what matters."""
    assert maturity_date(date(2024, 1, 1), maturity_days=0) == date(2024, 1, 1)
    assert is_mature(date(2024, 1, 1), as_of=date(2024, 1, 1), maturity_days=0) is True
