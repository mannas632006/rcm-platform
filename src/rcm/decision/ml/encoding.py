"""Feature rows to the matrix a model reads, and back to names (ADR-0026).

A category becomes its index in a vocabulary frozen on the training window.
A code first seen later -- a new payer, a CPT code the model never met -- is
*missing*, not a new index: the model has learned nothing about it, and a
made-up index would let it pretend it had. LightGBM sends missing values down
the branch it learned for them.

The vocabulary is stored with the model (`to_json`), so serving encodes
exactly as training did. So are a multiclass model's `classes` (ML-2): its
output columns are indices, and the names they stand for are part of the
model.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from rcm.analytics.dataset import Feature, Kind, Value
from rcm.platform.errors import ValidationFailedError


@dataclass(frozen=True, slots=True)
class Encoding:
    features: tuple[Feature, ...]
    #: Each CATEGORY feature's codes, in index order.
    vocabulary: Mapping[str, tuple[str, ...]]
    #: A multiclass model's answers, in output order. Empty for a binary one.
    classes: tuple[str, ...] = ()

    @classmethod
    def fit(cls, features: Sequence[Feature], rows: Iterable[Sequence[Value]]) -> Encoding:
        seen: dict[str, set[str]] = {f.name: set() for f in features if f.kind is Kind.CATEGORY}
        for values in rows:
            for feature, value in zip(features, values, strict=True):
                if feature.kind is Kind.CATEGORY and value is not None:
                    seen[feature.name].add(str(value))
        return cls(tuple(features), {name: tuple(sorted(codes)) for name, codes in seen.items()})

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.features]

    @property
    def categorical(self) -> list[int]:
        return [i for i, f in enumerate(self.features) if f.kind is Kind.CATEGORY]

    def matrix(self, rows: Sequence[Sequence[Value]]) -> NDArray[np.float64]:
        index = {
            name: {code: i for i, code in enumerate(codes)}
            for name, codes in self.vocabulary.items()
        }
        out = np.full((len(rows), len(self.features)), np.nan, dtype=np.float64)
        for r, values in enumerate(rows):
            if len(values) != len(self.features):
                raise ValidationFailedError(field="features", reason="row does not match the model")
            for c, (feature, value) in enumerate(zip(self.features, values, strict=True)):
                if value is None:
                    continue
                if feature.kind is Kind.CATEGORY:
                    code = index[feature.name].get(str(value))
                    if code is not None:
                        out[r, c] = code
                elif isinstance(value, float) and math.isfinite(value):
                    out[r, c] = value
        return out

    def with_classes(self, labels: Iterable[str]) -> Encoding:
        """This encoding, with the answers a multiclass model chooses among."""
        return Encoding(self.features, self.vocabulary, tuple(sorted(set(labels))))

    def class_index(self, labels: Sequence[str]) -> list[int]:
        index = {name: i for i, name in enumerate(self.classes)}
        unknown = sorted({label for label in labels if label not in index})
        if unknown:
            # Training learns the classes it saw; a later window naming
            # another cannot be scored as if it were one of them.
            raise ValidationFailedError(field="label", reason=f"classes not trained on: {unknown}")
        return [index[label] for label in labels]

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "features": [{"name": f.name, "kind": str(f.kind)} for f in self.features],
            "vocabulary": {name: list(codes) for name, codes in self.vocabulary.items()},
        }
        if self.classes:
            out["classes"] = list(self.classes)
        return out

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Encoding:
        features = tuple(Feature(f["name"], Kind(f["kind"])) for f in raw["features"])
        vocabulary = {name: tuple(codes) for name, codes in raw["vocabulary"].items()}
        return cls(features, vocabulary, tuple(raw.get("classes", ())))
