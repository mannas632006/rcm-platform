"""The thresholds a model decision turns on, from versioned config (M6.5).

    FR-ML-07 (MUST): Confidence thresholds are stored configuration,
    version-controlled, changeable only through a governed process. Never
    hard-coded (BR-HITL-03).

They live in `thresholds.toml` beside this module, so they ship with the code
that reads them and change only through a reviewed commit. Every prediction
records `Thresholds.version`.

The loader refuses rather than defaults (PD-18): a missing version, a missing
value, or a probability outside [0, 1] stops the caller. A threshold that
quietly fell back to some default would be exactly the hard-coded number
FR-ML-07 forbids, only harder to find.

`tests/unit/test_ml_thresholds.py` fails the build on any comparison against a
numeric literal in `rcm.decision.ml`, so this file is the only place the
numbers can come from.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from rcm.platform.errors import ValidationFailedError

THRESHOLDS_FILE: Final = Path(__file__).with_name("thresholds.toml")


@dataclass(frozen=True, slots=True)
class Drift:
    psi_warning: float
    psi_critical: float
    ece_critical: float
    min_coverage: float


@dataclass(frozen=True, slots=True)
class Thresholds:
    version: int
    #: False while every value is a placeholder (ADR-0026).
    decided: bool
    min_pr_auc_lift: float
    min_macro_f1_lift: float
    #: P(at or above which) a model routes to a person, by model name.
    review_at: Mapping[str, float]
    drift: Drift
    shadow_min_days: int

    def review_threshold(self, model: str) -> float:
        if model not in self.review_at:
            raise ValidationFailedError(field="thresholds", reason=f"no review_at for {model}")
        return self.review_at[model]


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name)
    if not isinstance(value, Mapping):
        raise ValidationFailedError(field="thresholds", reason=f"[{name}] is missing")
    return value


def _number(section: Mapping[str, Any], key: str, *, probability: bool = True) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationFailedError(field="thresholds", reason=f"{key} is missing or not a number")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (probability and number > 1):
        raise ValidationFailedError(field="thresholds", reason=f"{key} is out of range")
    return number


def parse(raw: Mapping[str, Any]) -> Thresholds:
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationFailedError(field="thresholds", reason="version is missing")
    decided = raw.get("decided")
    if not isinstance(decided, bool):
        raise ValidationFailedError(field="thresholds", reason="say whether these are decided")
    models = _section(raw, "models")
    drift = _section(raw, "drift")
    shadow = _section(raw, "shadow")
    days = shadow.get("min_days")
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValidationFailedError(field="thresholds", reason="shadow min_days is missing")
    return Thresholds(
        version=version,
        decided=decided,
        min_pr_auc_lift=_number(_section(raw, "acceptance"), "min_pr_auc_lift"),
        min_macro_f1_lift=_number(_section(raw, "acceptance"), "min_macro_f1_lift"),
        review_at={name: _number(_section(models, name), "review_at") for name in sorted(models)},
        drift=Drift(
            psi_warning=_number(drift, "psi_warning", probability=False),
            psi_critical=_number(drift, "psi_critical", probability=False),
            ece_critical=_number(drift, "ece_critical"),
            min_coverage=_number(drift, "min_coverage"),
        ),
        shadow_min_days=days,
    )


@lru_cache(maxsize=1)
def load() -> Thresholds:
    """The thresholds this build ships with."""
    with THRESHOLDS_FILE.open("rb") as handle:
        return parse(tomllib.load(handle))
