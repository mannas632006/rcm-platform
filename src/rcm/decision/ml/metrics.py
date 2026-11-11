"""Calibration and stability measures, in NumPy only (M6.2, M6.7).

These run where a model is served and monitored, so they cannot lean on the
training-time extras. Each takes plain arrays and returns a float; what counts
as too much is in `thresholds.toml`, never here.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike

#: Ten equal-width bins: deciles of predicted probability.
CALIBRATION_BINS = 10


def expected_calibration_error(
    probabilities: ArrayLike, outcomes: ArrayLike, *, bins: int = CALIBRATION_BINS
) -> float:
    """Mean |predicted - observed| across probability bins, weighted by count.

    A model that says 0.3 should be right about 30% of the time. Zero is
    perfect; it is reported, and alarmed on (M6.7), not optimised.
    """
    p = np.asarray(probabilities, dtype=np.float64)
    y = np.asarray(outcomes, dtype=np.float64)
    if p.size == 0:
        return 0.0
    which = np.minimum((p * bins).astype(int), bins - 1)
    total = 0.0
    for b in range(bins):
        mask = which == b
        if mask.any():
            total += float(mask.sum()) * abs(float(p[mask].mean()) - float(y[mask].mean()))
    return total / p.size


def population_stability_index(
    expected: ArrayLike, actual: ArrayLike, *, edges: ArrayLike, floor: float = 1e-4
) -> float:
    """PSI between two samples of one quantity, over the given bin edges.

    Sum over bins of (a - e) * ln(a / e), with each share floored so an empty
    bin does not divide by zero. The edges come from the reference (training)
    sample, so a shift is measured against what the model learned on.
    """
    bins = np.asarray(edges, dtype=np.float64)
    e = np.histogram(np.asarray(expected, dtype=np.float64), bins=bins)[0].astype(np.float64)
    a = np.histogram(np.asarray(actual, dtype=np.float64), bins=bins)[0].astype(np.float64)
    if e.sum() == 0 or a.sum() == 0:
        return 0.0
    e = np.maximum(e / e.sum(), floor)
    a = np.maximum(a / a.sum(), floor)
    return float(np.sum((a - e) * np.log(a / e)))


def stability_index_of_counts(
    expected: ArrayLike, actual: ArrayLike, *, floor: float = 1e-4
) -> float:
    """PSI between two samples already counted into the same bins.

    For a quantity that is not a number -- a payer, a procedure code -- and
    for the share of rows where a feature is missing, which no histogram
    edge can hold.
    """
    e = np.asarray(expected, dtype=np.float64)
    a = np.asarray(actual, dtype=np.float64)
    if e.sum() == 0 or a.sum() == 0:
        return 0.0
    e = np.maximum(e / e.sum(), floor)
    a = np.maximum(a / a.sum(), floor)
    return float(np.sum((a - e) * np.log(a / e)))
