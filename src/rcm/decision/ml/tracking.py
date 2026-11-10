"""Experiment tracking in MLflow (M6.1, FR-ML-03).

Training-time only: MLflow is in the `ml` extra, and nothing a request
reaches imports this module.

MLflow keeps the experiment history: every run, accepted or not, with its
hyperparameters, metrics and the model text. `ml_model_version` keeps what a
running system may load. Each points at the other (`mlflow_run_id`, and the
`model_version_id` tag).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class Tracker(Protocol):
    def record(
        self,
        *,
        experiment: str,
        params: Mapping[str, Any],
        metrics: Mapping[str, float],
        tags: Mapping[str, str],
        artefact: str,
    ) -> str | None:
        """Record one run; return its id."""
        ...


class MlflowTracker:
    def __init__(self, tracking_uri: str) -> None:
        self.tracking_uri = tracking_uri

    def record(
        self,
        *,
        experiment: str,
        params: Mapping[str, Any],
        metrics: Mapping[str, float],
        tags: Mapping[str, str],
        artefact: str,
    ) -> str:
        import mlflow  # the ml extra; imported only when a run is recorded

        mlflow.set_tracking_uri(self.tracking_uri)
        mlflow.set_experiment(experiment)
        with mlflow.start_run() as run:
            mlflow.log_params({k: str(v) for k, v in params.items()})
            mlflow.log_metrics(dict(metrics))
            mlflow.set_tags(dict(tags))
            mlflow.log_text(artefact, "model.txt")
            return str(run.info.run_id)


def flat_metrics(report: Mapping[str, Any], *, prefix: str = "") -> dict[str, float]:
    """The numeric leaves of a nested report, as MLflow wants them."""
    out: dict[str, float] = {}
    for key, value in report.items():
        name = f"{prefix}{key}".replace(" ", "_")
        if isinstance(value, Mapping):
            out.update(flat_metrics(value, prefix=f"{name}."))
        elif isinstance(value, int | float) and not isinstance(value, bool):
            out[name] = float(value)
    return out
