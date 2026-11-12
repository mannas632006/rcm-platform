"""A model version's status in service: shadow, active, reverted (M6.6, M6.8).

    FR-ML-04 (MUST): No model reaches production without a shadow period.
    FR-ML-08 (MUST): Critical drift triggers automatic reversion to
    rules-only operation.

Every change is a new row in `ml_deployment`; a version's status is its
latest row. Nothing here is a flag in memory: a process that restarts reads
the same status every other process does, so a reverted model stays reverted.

Only an ACCEPTED version may enter shadow, and only a version that has been
in shadow for `shadow.min_days` (thresholds) may be promoted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.decision.ml import thresholds
from rcm.decision.ml.registry_models import ModelVersion, Verdict
from rcm.decision.ml.serving_models import Deployment, Status
from rcm.platform.errors import ValidationFailedError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

#: Statuses in which a version is scored.
SERVING = frozenset({str(Status.SHADOW), str(Status.ACTIVE)})


def states(session: Session, model_name: str) -> dict[int, Deployment]:
    """Each deployed version's latest row."""
    rows = session.scalars(
        select(Deployment)
        .where(Deployment.model_name == model_name)
        .order_by(Deployment.deployment_id)
    ).all()
    return {row.version: row for row in rows}


def latest(session: Session, model_name: str) -> Deployment | None:
    """The most recent change to any version of this model."""
    return session.scalar(
        select(Deployment)
        .where(Deployment.model_name == model_name)
        .order_by(Deployment.deployment_id.desc())
        .limit(1)
    )


def _change(
    session: Session,
    *,
    model_name: str,
    version: int,
    status: Status,
    reason: str,
    changed_by: str,
    now: datetime,
) -> Deployment:
    row = Deployment(
        model_name=model_name,
        version=version,
        status=str(status),
        reason=reason,
        changed_by=changed_by,
        changed_at=now,
    )
    session.add(row)
    session.flush()
    log.info(
        "ml.deployment_changed",
        model=model_name,
        version=version,
        status=str(status),
        changed_by=changed_by,
    )
    return row


def accepted_version(session: Session, model_name: str, version: int) -> ModelVersion:
    """The registry row, refused unless it was ACCEPTED when trained."""
    row = session.scalar(
        select(ModelVersion).where(
            ModelVersion.model_name == model_name, ModelVersion.version == version
        )
    )
    if row is None:
        raise ValidationFailedError(
            field="version", reason=f"{model_name} v{version} is not registered"
        )
    if row.verdict != str(Verdict.ACCEPTED):
        raise ValidationFailedError(
            field="version", reason=f"{model_name} v{version} was {row.verdict}: {row.reason}"
        )
    return row


def start_shadow(
    session: Session, *, model_name: str, version: int, changed_by: str, reason: str, now: datetime
) -> Deployment:
    """Put an ACCEPTED version in shadow: scored and recorded, never used."""
    accepted_version(session, model_name, version)
    current = states(session, model_name).get(version)
    if current is not None and current.status in SERVING:
        raise ValidationFailedError(
            field="version", reason=f"{model_name} v{version} is already {current.status}"
        )
    return _change(
        session,
        model_name=model_name,
        version=version,
        status=Status.SHADOW,
        reason=reason,
        changed_by=changed_by,
        now=now,
    )


def promote(
    session: Session, *, model_name: str, version: int, changed_by: str, reason: str, now: datetime
) -> Deployment:
    """Make a shadowed version the one whose scores are used (FR-ML-04)."""
    current = states(session, model_name)
    mine = current.get(version)
    if mine is None or mine.status != str(Status.SHADOW):
        raise ValidationFailedError(
            field="version",
            reason=f"{model_name} v{version} is not in shadow; no model is promoted without it",
        )
    required = timedelta(days=thresholds.load().shadow_min_days)
    served = now - mine.changed_at
    if served < required:
        raise ValidationFailedError(
            field="version",
            reason=f"{model_name} v{version} has been in shadow {served.days} days of "
            f"the {required.days} required",
        )
    for other in current.values():
        if other.status == str(Status.ACTIVE):
            _change(
                session,
                model_name=model_name,
                version=other.version,
                status=Status.RETIRED,
                reason=f"replaced by v{version}",
                changed_by=changed_by,
                now=now,
            )
    return _change(
        session,
        model_name=model_name,
        version=version,
        status=Status.ACTIVE,
        reason=reason,
        changed_by=changed_by,
        now=now,
    )


def revert(
    session: Session, *, model_name: str, version: int, changed_by: str, reason: str, now: datetime
) -> Deployment:
    """Withdraw a serving version. What it informed runs on rules only."""
    mine = states(session, model_name).get(version)
    if mine is None or mine.status not in SERVING:
        raise ValidationFailedError(
            field="version", reason=f"{model_name} v{version} is not serving"
        )
    return _change(
        session,
        model_name=model_name,
        version=version,
        status=Status.REVERTED,
        reason=reason,
        changed_by=changed_by,
        now=now,
    )
