"""Register trained model versions, and load them back (M6.1, ADR-0026).

`ml_model_version` is insert-only by grant, so this module only adds rows and
reads them. A version's status in service (shadow, active, reverted) is
M6.6's, in its own table; here a version is ACCEPTED or REJECTED once, when
it is trained.

**A model trained on synthetic data never runs in production** (CLAUDE.md
section 8). `load` refuses a SYNTHETIC version when the environment is
production. The synthetic corpus contains only the denial patterns its
generator was told to produce; a model that learned them has learned the
generator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.decision.ml.encoding import Encoding
from rcm.decision.ml.model import Model
from rcm.decision.ml.registry_models import DataSource, ModelVersion, Verdict
from rcm.platform.config import get_settings
from rcm.platform.errors import ValidationFailedError


@dataclass(frozen=True, slots=True)
class CodeVersion:
    """The code a model was trained by (FR-ML-03)."""

    commit: str
    #: Uncommitted changes in the tree: the commit alone will not rebuild it.
    dirty: bool


def next_version(session: Session, model_name: str) -> int:
    latest = session.scalar(
        select(func.max(ModelVersion.version)).where(ModelVersion.model_name == model_name)
    )
    return (latest or 0) + 1


def register(
    session: Session,
    *,
    model_name: str,
    model: Model,
    verdict: Verdict,
    reason: str,
    data_source: DataSource,
    data_hash: str,
    code: CodeVersion,
    thresholds_version: int,
    params: dict[str, Any],
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    windows: dict[str, Any],
    trained_by: str,
    mlflow_run_id: str | None = None,
) -> ModelVersion:
    """Record one trained version in the caller's transaction."""
    if not trained_by.strip():
        raise ValidationFailedError(field="trained_by", reason="name who trained it")
    row = ModelVersion(
        model_name=model_name,
        version=next_version(session, model_name),
        verdict=str(verdict),
        reason=reason,
        data_source=str(data_source),
        data_hash=data_hash,
        code_commit=code.commit,
        code_dirty=code.dirty,
        thresholds_version=thresholds_version,
        params=params,
        metrics=metrics,
        baseline=baseline,
        windows=windows,
        encoding=model.encoding.to_json(),
        artefact=model.text(),
        mlflow_run_id=mlflow_run_id,
        trained_by=trained_by,
    )
    session.add(row)
    session.flush()
    return row


def latest_accepted(session: Session, model_name: str) -> ModelVersion | None:
    return session.scalar(
        select(ModelVersion)
        .where(
            ModelVersion.model_name == model_name,
            ModelVersion.verdict == str(Verdict.ACCEPTED),
        )
        .order_by(ModelVersion.version.desc())
        .limit(1)
    )


def load(row: ModelVersion) -> Model:
    """The model a registry row holds, refused where it may not run."""
    if get_settings().environment == "production" and row.data_source == str(DataSource.SYNTHETIC):
        raise ValidationFailedError(
            field="model_version",
            reason=f"{row.model_name} v{row.version} was trained on synthetic data; "
            "it cannot run in production (CLAUDE.md section 8)",
        )
    return Model.from_text(row.artefact, Encoding.from_json(row.encoding))
