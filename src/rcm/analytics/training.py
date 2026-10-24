"""Which rows may train, and how they are split (M2.5, M2.6).

Two rules, both of which exist because breaking them produces a model that
looks excellent and is wrong.

    DR-08 (MUST): Train/test splits MUST be temporal, never random. Random
    splits leak payer behaviour across the boundary and inflate measured
    accuracy.

    DR-09 (MUST): No row enters final-outcome training until
    `outcome_matured_at` has passed, because reversals and recoupments change
    outcomes retroactively.

**Why random splitting is not an option rather than a discouraged one.**
A random split puts claims from the same week on both sides of the boundary.
Payer behaviour moves in time -- a policy changes, an edit is switched on, a
backlog clears -- so a random test set is drawn from the same regime as the
training set, and the model is scored on conditions it has already seen. The
number that comes out is higher than the truth and nothing about it looks
wrong. Offering `shuffle=True` anywhere in this module would mean that one
day, under deadline, somebody passes it.

So `split()` takes a strategy and refuses every value but the temporal one,
including the ones a reasonable person would try (`"random"`, `"stratified"`,
`"kfold"`). Refusing by name rather than silently lacking the feature is the
difference between an error a developer reads and a function they go around.

**Why maturity is a gate and not a filter applied later.**
A claim paid in March and recouped in September was, in April, a paid claim.
Training on April's view teaches the model that it was paid, and it was not.
`outcome_matured_at` is the date after which the answer stops moving, and
`[TBD-50]` -- the length of that window -- is the sponsor's decision, not
this module's. It is a parameter with a labelled default, never a literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Final

from rcm.platform.errors import LeakageError

#: [TBD-50] How long after the date of service an outcome is final.
#:
#: DR-09 recommends 180 days. That is a recommendation in a specification,
#: not a measurement of this book of business, and it is wrong in both
#: directions for some payers. It stays a named placeholder until the sponsor
#: decides, and every caller takes it as an argument so the decision lands in
#: one place when it is made.
DEFAULT_MATURITY_DAYS: Final = 180


class SplitStrategy(StrEnum):
    """How a dataset may be divided.

    One member, deliberately. The enum exists so the refusal below has
    something to name, not because a second strategy is coming.
    """

    TEMPORAL = "TEMPORAL"


#: Strategies a caller might reasonably ask for, each of which leaks. Named
#: so the error can say *why* rather than "invalid value", because a
#: developer who is told "random is not supported" goes looking for the
#: supported one, and a developer told "random leaks payer behaviour across
#: the boundary" stops.
_REFUSED: Final[dict[str, str]] = {
    "RANDOM": "a random split draws test rows from the same time period as training rows, "
    "so the model is scored on payer behaviour it has already seen",
    "SHUFFLE": "shuffling destroys the time ordering a temporal split depends on",
    "STRATIFIED": "stratifying on the outcome balances classes across the boundary, which "
    "is a random split with extra steps",
    "KFOLD": "k-fold puts future rows in the training set for every fold but the last",
    "GROUP": "grouping by claim still mixes time periods unless the groups are temporal",
}


@dataclass(frozen=True, slots=True)
class TemporalSplit:
    """Where the boundary falls, and how much landed on each side."""

    cutoff: date
    train_rows: int
    test_rows: int

    @property
    def total(self) -> int:
        return self.train_rows + self.test_rows

    @property
    def is_usable(self) -> bool:
        """Both sides non-empty.

        A split with nothing on one side is not a split, and a training run
        that proceeds from one is measuring nothing -- which is worth
        reporting rather than discovering from a suspiciously round score.
        """
        return self.train_rows > 0 and self.test_rows > 0


def split(
    *,
    strategy: str | SplitStrategy,
    cutoff: date | None = None,
) -> SplitStrategy:
    """Validate a requested split strategy, or refuse it by name (DR-08).

    Returns the strategy when it is the temporal one, so a caller can write
    `strategy = split(strategy=requested, cutoff=c)` and have the refusal
    happen before any data is read.
    """
    requested = str(strategy).strip().upper()

    if requested in _REFUSED:
        raise LeakageError(strategy=requested, reason=_REFUSED[requested])

    if requested != SplitStrategy.TEMPORAL:
        raise LeakageError(
            strategy=requested,
            reason="the only split this platform performs is temporal (DR-08)",
        )

    if cutoff is None:
        raise LeakageError(
            strategy=requested,
            reason="a temporal split needs a cutoff date; without one there is no boundary",
        )

    return SplitStrategy.TEMPORAL


def maturity_date(date_of_service: date, *, maturity_days: int = DEFAULT_MATURITY_DAYS) -> date:
    """When this row's outcome stops moving (DR-09).

    Measured from the date of service rather than from adjudication, because
    a recoupment is timed against the service, not against the cheque that
    paid for it.
    """
    if maturity_days < 0:
        raise LeakageError(
            maturity_days=maturity_days,
            reason="a negative maturity window would mature rows before they happened",
        )
    return date_of_service + timedelta(days=maturity_days)


def is_mature(
    date_of_service: date | None,
    *,
    as_of: date,
    maturity_days: int = DEFAULT_MATURITY_DAYS,
) -> bool:
    """May this row be used for final-outcome training?

    A row with no date of service cannot be aged, so it is **not** mature.
    Fail closed (PD-18): treating an unknown date as mature admits exactly
    the rows nothing can vouch for.
    """
    if date_of_service is None:
        return False
    return as_of >= maturity_date(date_of_service, maturity_days=maturity_days)
