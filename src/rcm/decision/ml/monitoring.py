"""The drift monitor, and the auto-revert it can pull (M6.7, M6.8; ADR-0028).

    FR-ML-08 (MUST): Critical drift triggers automatic reversion to
    rules-only operation. The system must degrade to a known-safe state
    without waiting for a human.

For each serving version, over the last `drift.window_days` of predictions:

- **Input drift.** The population stability index of every feature, recent
  inputs against the rows the model trained on. A number is binned on the
  training deciles; a code on the training window's commonest codes, with
  one bin for every other code; and "missing" is a bin of its own.
- **Output drift.** PSI of the scores, against the model's scores on its
  training rows.
- **Calibration and accuracy.** Where the outcome has since matured: the
  expected calibration error and the Brier score.
- **Coverage.** The share of requests that got a score rather than
  DEGRADED.

A CRITICAL breach reverts the version in the same transaction that records
the run: `ml_deployment` gains a REVERTED row by `monitor:auto`, and from the
next request `serving.score` answers "withdrawn" and scores nothing. No
person is asked first.

Every limit is read from `thresholds.toml` (FR-ML-07). Below
`drift.min_predictions` nothing is judged: the run says so and changes
nothing.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.analytics.dataset import (
    Feature,
    Kind,
    LineDataset,
    Value,
    denial_lines,
    recovery_lines,
)
from rcm.decision.ml import deployment, registry, thresholds
from rcm.decision.ml.metrics import (
    CALIBRATION_BINS,
    expected_calibration_error,
    population_stability_index,
    stability_index_of_counts,
)
from rcm.decision.ml.serving_models import (
    DENIAL_RISK,
    RECOVERY,
    MonitorAction,
    MonitorRun,
    Prediction,
    PredictionStatus,
    Severity,
)
from rcm.platform.audit import DecisionRecord
from rcm.platform.logging import get_logger

log = get_logger(__name__)

MONITOR: Final = "monitor:auto"

#: Bins a feature is compared over: deciles for a number, the commonest
#: codes for a category. How a quantity is measured, not a limit on it.
BINS: Final = 10

#: The matured rows each model trained on, and is judged against.
TRAINING: Final[Mapping[str, Callable[[Session], LineDataset[bool]]]] = {
    DENIAL_RISK: lambda session: denial_lines(session),
    RECOVERY: lambda session: recovery_lines(session),
}


@dataclass(frozen=True, slots=True)
class Breach:
    metric: str
    value: float
    limit: float
    severity: Severity

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "limit": self.limit,
            "severity": str(self.severity),
        }


def feature_psi(feature: Feature, reference: Sequence[Value], recent: Sequence[Value]) -> float:
    """PSI of one feature, recent against reference, missing included."""
    if feature.kind is Kind.CATEGORY:
        common = [
            code
            for code, _ in Counter(str(v) for v in reference if v is not None).most_common(BINS - 1)
        ]
        index = {code: i for i, code in enumerate(common)}

        def counts(values: Sequence[Value]) -> list[int]:
            # One bin per common code, one for every other code, one for missing.
            out = [0] * (len(common) + 2)
            for value in values:
                if value is None:
                    out[-1] += 1
                else:
                    out[index.get(str(value), len(common))] += 1
            return out

        return stability_index_of_counts(counts(reference), counts(recent))

    known = np.asarray([v for v in reference if isinstance(v, float)], dtype=np.float64)
    inner = (
        np.unique(np.quantile(known, np.linspace(0, 1, BINS + 1)[1:-1])) if known.size else known
    )

    def binned(values: Sequence[Value]) -> list[int]:
        numbers = np.asarray([v for v in values if isinstance(v, float)], dtype=np.float64)
        out = np.bincount(np.searchsorted(inner, numbers, side="right"), minlength=inner.size + 1)
        return [*out.tolist(), len(values) - int(numbers.size)]

    return stability_index_of_counts(binned(reference), binned(recent))


def _severity(breaches: Sequence[Breach]) -> Severity:
    if any(b.severity is Severity.CRITICAL for b in breaches):
        return Severity.CRITICAL
    return Severity.WARNING if breaches else Severity.OK


def measure(
    *,
    features: Sequence[Feature],
    reference: Sequence[Sequence[Value]],
    reference_scores: Sequence[float],
    recent: Sequence[Sequence[Value]],
    recent_scores: Sequence[float],
    realised: Sequence[tuple[float, bool]],
    requests: int,
    config: thresholds.Drift,
) -> tuple[dict[str, Any], list[Breach]]:
    """What one window measures, and which limits it breaks. Pure: no I/O."""
    metrics: dict[str, Any] = {
        "window_days": config.window_days,
        "requests": requests,
        "scored": len(recent),
        "realised": len(realised),
    }
    breaches: list[Breach] = []
    judged = requests >= config.min_predictions
    metrics["judged"] = judged
    if not judged:
        return metrics, breaches

    coverage = len(recent) / requests
    metrics["coverage"] = coverage
    if coverage < config.min_coverage:
        breaches.append(Breach("coverage", coverage, config.min_coverage, Severity.CRITICAL))

    if len(recent) >= config.min_predictions:
        by_feature = {
            f.name: feature_psi(f, [r[i] for r in reference], [r[i] for r in recent])
            for i, f in enumerate(features)
        }
        edges = np.linspace(0.0, 1.0, CALIBRATION_BINS + 1)
        by_feature_and_output = {
            **{f"input:{name}": value for name, value in by_feature.items()},
            "output:score": population_stability_index(
                reference_scores, recent_scores, edges=edges
            ),
        }
        metrics["input_psi"] = by_feature
        metrics["output_psi"] = by_feature_and_output["output:score"]
        for name, value in by_feature_and_output.items():
            if value >= config.psi_critical:
                breaches.append(Breach(name, value, config.psi_critical, Severity.CRITICAL))
            elif value >= config.psi_warning:
                breaches.append(Breach(name, value, config.psi_warning, Severity.WARNING))

    if len(realised) >= config.min_predictions:
        scores = [s for s, _ in realised]
        outcomes = [1.0 if y else 0.0 for _, y in realised]
        ece = expected_calibration_error(scores, outcomes)
        metrics["ece"] = ece
        metrics["brier"] = float(np.mean((np.asarray(scores) - np.asarray(outcomes)) ** 2))
        if ece > config.ece_critical:
            breaches.append(Breach("calibration:ece", ece, config.ece_critical, Severity.CRITICAL))
    return metrics, breaches


def _as_value(raw: object, kind: Kind) -> Value:
    """A feature as it was recorded with its prediction, as a model reads it."""
    if raw is None:
        return None
    if kind is Kind.CATEGORY:
        return str(raw)
    return float(raw) if isinstance(raw, int | float) else None


def _window(dataset: LineDataset[bool], windows: Mapping[str, str]) -> LineDataset[bool]:
    return dataset.window(
        date.fromisoformat(windows["train_start"]), date.fromisoformat(windows["valid_start"])
    )


def run(session: Session, *, model_name: str, version: int, now: datetime) -> MonitorRun:
    """Measure one serving version, record it, and revert it if it must be."""
    config = thresholds.load()
    row = deployment.accepted_version(session, model_name, version)
    model = registry.load(row)
    features = model.encoding.features

    since = now - timedelta(days=config.drift.window_days)
    # An outer join: a DEGRADED prediction has no decision record to bring.
    predictions: list[tuple[Prediction, dict[str, Any] | None]] = [
        (found[0], found[1])
        for found in session.execute(
            select(Prediction, DecisionRecord.input_snapshot)
            .outerjoin(DecisionRecord, DecisionRecord.decision_id == Prediction.decision_id)
            .where(
                Prediction.model_version_id == row.model_version_id,
                Prediction.predicted_at >= since,
            )
        )
    ]
    scored: list[tuple[Prediction, dict[str, Any]]] = [
        (p, snapshot["features"])
        for p, snapshot in predictions
        if p.status == str(PredictionStatus.SCORED) and snapshot is not None
    ]
    recent = [
        tuple(_as_value(inputs.get(f.name), f.kind) for f in features) for _, inputs in scored
    ]
    recent_scores = [float(p.score) for p, _ in scored if p.score is not None]

    dataset = TRAINING[model_name](session)
    reference = [r.values for r in _window(dataset, row.windows).rows]
    answers: dict[uuid.UUID, bool] = {r.service_line_id: r.label for r in dataset.rows}
    realised = [
        (float(p.score), answers[p.service_line_id])
        for p, _ in scored
        if p.score is not None and p.service_line_id in answers
    ]

    metrics, breaches = measure(
        features=features,
        reference=reference,
        reference_scores=model.predict(reference).tolist(),
        recent=recent,
        recent_scores=recent_scores,
        realised=realised,
        requests=len(predictions),
        config=config.drift,
    )
    severity = _severity(breaches)
    current = deployment.states(session, model_name).get(version)
    serving = current is not None and current.status in deployment.SERVING
    action = MonitorAction.NONE
    if severity is Severity.CRITICAL and serving:
        worst = "; ".join(
            f"{b.metric} {b.value:.4f} against {b.limit:.4f}"
            for b in breaches
            if b.severity is Severity.CRITICAL
        )
        deployment.revert(
            session,
            model_name=model_name,
            version=version,
            changed_by=MONITOR,
            reason=f"critical drift: {worst}",
            now=now,
        )
        action = MonitorAction.REVERTED
        log.error("ml.auto_reverted", model=model_name, version=version, breaches=len(breaches))
    elif breaches:
        log.warning("ml.drift", model=model_name, version=version, severity=str(severity))

    record = MonitorRun(
        model_name=model_name,
        version=version,
        severity=str(severity),
        action=str(action),
        thresholds_version=config.version,
        metrics=metrics,
        breaches=[b.as_dict() for b in breaches],
        ran_at=now,
    )
    session.add(record)
    session.flush()
    return record


def run_all(session: Session, *, now: datetime) -> list[MonitorRun]:
    """The daily monitor: every serving version of every served model."""
    runs: list[MonitorRun] = []
    for model_name in TRAINING:
        for version, state in sorted(deployment.states(session, model_name).items()):
            if state.status in deployment.SERVING:
                runs.append(run(session, model_name=model_name, version=version, now=now))
    return runs
