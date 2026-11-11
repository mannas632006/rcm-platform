"""How a model scored on the test window (M6.2, M6.3, FR-ML-03).

Training-time only: scikit-learn is in the `ml` extra.

PR-AUC (average precision) is the number a model is accepted on. Denials are
the minority class, and ROC-AUC flatters a model on a rare class: it rewards
ranking the many clean claims below each other, which nobody needs. PR-AUC
asks only how well the denials are found and how many good claims come with
them. The rest is reported so a reviewer can see what the one number hides.

ML-2 chooses a category, and its rival, the taxonomy table, gives an answer
rather than a probability. They are compared on macro-F1: every category
counts the same, so a model cannot win by being right only about the common
ones.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    f1_score,
    roc_auc_score,
)

from rcm.decision.ml.metrics import expected_calibration_error


def _both_classes(labels: Sequence[bool]) -> bool:
    return any(labels) and not all(labels)


def binary_report(
    labels: Sequence[bool], scores: Sequence[float], *, payers: Sequence[str | None]
) -> dict[str, Any]:
    """Every number for one set of scores, overall and per payer."""
    y = np.asarray(labels, dtype=np.float64)
    p = np.asarray(scores, dtype=np.float64)
    report: dict[str, Any] = {
        "rows": int(y.size),
        "positives": int(y.sum()),
        "prevalence": float(y.mean()) if y.size else None,
        "pr_auc": float(average_precision_score(y, p)) if _both_classes(labels) else None,
        "roc_auc": float(roc_auc_score(y, p)) if _both_classes(labels) else None,
        "brier": float(brier_score_loss(y, p)) if y.size else None,
        "ece": expected_calibration_error(p, y),
    }
    by_payer: dict[str, list[int]] = defaultdict(list)
    for i, payer in enumerate(payers):
        by_payer[payer or "unknown"].append(i)
    report["per_payer"] = {
        payer: {
            "rows": len(rows),
            "positives": int(y[rows].sum()),
            "pr_auc": (
                float(average_precision_score(y[rows], p[rows]))
                if _both_classes([labels[i] for i in rows])
                else None
            ),
        }
        for payer, rows in sorted(by_payer.items())
    }
    return report


def _macro_f1(truth: Sequence[str], answers: Sequence[str]) -> float | None:
    # Averaged over the categories that occur, so an answer no line has
    # (the baseline's UNMAPPED) is a miss, not a category of its own.
    labels = sorted(set(truth))
    if not labels:
        return None
    return float(f1_score(truth, answers, labels=labels, average="macro", zero_division=0))


def multiclass_report(
    labels: Sequence[str], answers: Sequence[str], *, payers: Sequence[str | None]
) -> dict[str, Any]:
    """Every number for one set of answers, overall, per class and per payer."""
    report: dict[str, Any] = {
        "rows": len(labels),
        "classes": len(set(labels)),
        "macro_f1": _macro_f1(labels, answers),
        "accuracy": float(accuracy_score(labels, answers)) if labels else None,
    }
    per_class = f1_score(labels, answers, labels=sorted(set(labels)), average=None, zero_division=0)
    report["per_class"] = {
        name: {"rows": labels.count(name), "f1": float(score)}
        for name, score in zip(sorted(set(labels)), per_class, strict=True)
    }
    by_payer: dict[str, list[int]] = defaultdict(list)
    for i, payer in enumerate(payers):
        by_payer[payer or "unknown"].append(i)
    report["per_payer"] = {
        payer: {
            "rows": len(rows),
            "macro_f1": _macro_f1([labels[i] for i in rows], [answers[i] for i in rows]),
        }
        for payer, rows in sorted(by_payer.items())
    }
    return report
