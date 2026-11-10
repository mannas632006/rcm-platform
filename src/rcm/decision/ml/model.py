"""A LightGBM model with its encoding: fit it, score with it, explain it.

Deterministic by construction (ADR-0026): a fixed seed, `deterministic`, one
thread and row-wise histograms. The same rows and the same code give the same
model, byte for byte, which is what lets PD-10 treat a model as a layer
rebuildable from RAW and code, and lets a data hash mean something.

Explanations are SHAP values from LightGBM's own TreeSHAP
(`pred_contrib=True`): exact for trees, and with no dependency beyond the
model. `tests/unit/test_ml_model.py` checks them against the `shap` package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

from rcm.analytics.dataset import Value
from rcm.decision.ml.encoding import Encoding

#: Hyperparameters for a binary classifier. Recorded with every version
#: (FR-ML-03). These shape how the model learns; no decision is taken on
#: them, so they are not thresholds (FR-ML-07).
BINARY_PARAMS: Final[Mapping[str, Any]] = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.9,
    "lambda_l2": 1.0,
    "max_cat_to_onehot": 8,
    "cat_smooth": 10.0,
    "seed": 20261110,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,
    "verbosity": -1,
}
MAX_ROUNDS: Final = 1000
EARLY_STOPPING_ROUNDS: Final = 50


@dataclass(frozen=True, slots=True)
class Driver:
    """One feature's push on one prediction, in log-odds."""

    feature: str
    contribution: float


@dataclass(frozen=True, slots=True)
class Model:
    booster: lgb.Booster
    encoding: Encoding

    def text(self) -> str:
        return str(self.booster.model_to_string())

    @classmethod
    def from_text(cls, text: str, encoding: Encoding) -> Model:
        return cls(lgb.Booster(model_str=text), encoding)

    def predict(self, rows: Sequence[Sequence[Value]]) -> NDArray[np.float64]:
        """P(positive) for each row."""
        if not rows:
            return np.zeros(0, dtype=np.float64)
        return np.asarray(self.booster.predict(self.encoding.matrix(rows)), dtype=np.float64)

    def drivers(self, rows: Sequence[Sequence[Value]], *, top: int) -> list[list[Driver]]:
        """Each row's `top` features by absolute SHAP value, largest first."""
        if not rows:
            return []
        contributions = np.asarray(
            self.booster.predict(self.encoding.matrix(rows), pred_contrib=True), dtype=np.float64
        )
        names = self.encoding.names
        out: list[list[Driver]] = []
        for row in contributions:
            # The last column is the expected value (the bias), not a feature.
            order = np.argsort(-np.abs(row[:-1]), kind="stable")[:top]
            out.append([Driver(names[i], float(row[i])) for i in order])
        return out


def fit_binary(
    encoding: Encoding,
    train: tuple[Sequence[Sequence[Value]], Sequence[bool]],
    valid: tuple[Sequence[Sequence[Value]], Sequence[bool]],
    *,
    params: Mapping[str, Any] = BINARY_PARAMS,
) -> Model:
    """Train on `train`, stop early on `valid`, and return the best iteration."""
    names = encoding.names
    categorical = encoding.categorical
    train_set = lgb.Dataset(
        encoding.matrix(train[0]),
        label=np.asarray(train[1], dtype=np.float64),
        feature_name=names,
        categorical_feature=categorical,
        free_raw_data=False,
    )
    valid_set = lgb.Dataset(
        encoding.matrix(valid[0]),
        label=np.asarray(valid[1], dtype=np.float64),
        reference=train_set,
        feature_name=names,
        categorical_feature=categorical,
        free_raw_data=False,
    )
    booster = lgb.train(
        dict(params),
        train_set,
        num_boost_round=MAX_ROUNDS,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )
    # Keep only the trees up to the best validation round, so what is stored
    # is what was measured.
    best = booster.best_iteration or booster.current_iteration()
    trimmed = lgb.Booster(model_str=booster.model_to_string(num_iteration=best))
    return Model(trimmed, encoding)
