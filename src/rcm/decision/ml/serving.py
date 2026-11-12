"""Score service lines with a deployed model, and record every score (M6.4,
M6.6, M6.9; ADR-0028).

    FR-ML-05 (MUST): Every prediction persists a `decision_record` with model
    version, inputs, output, confidence, and SHAP drivers.
    FR-ML-06 (MUST): Inference degrades gracefully. Missing features never
    produce a guessed prediction -- they route to rules or humans.

`score` is the one way a model is asked anything:

- **Which versions answer** is read from `ml_deployment` on every call. A
  version in SHADOW is scored and recorded, and its scores are never
  returned (FR-ML-04). Only the ACTIVE version's are. With none serving,
  nothing is scored and the caller is told why; that is the rules-only path.
- **The inputs are the mart's**, the row the model's training rows came
  from, read through the same leakage gate (PD-11).
- **A line missing a feature the model was never trained without is not
  scored.** It is recorded as DEGRADED with the names missing, and the
  caller gets no score for it: rules, or a person, decide that line.
- **A line is scored once per model version.** The score, its inputs and its
  drivers are written the first time; later calls read them back, so opening
  a queue twice does not make two decisions of one.

Nothing here imports scikit-learn or MLflow: a request reaches this module.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.analytics.dataset import (
    DENIAL_RISK_FEATURES,
    OPTIONAL_FEATURES,
    RECOVERY_FEATURES,
    Feature,
    LineRow,
    claims_in_warehouse,
    serving_lines,
)
from rcm.analytics.features import Stage
from rcm.decision.ml import deployment, registry, thresholds
from rcm.decision.ml.model import Driver, Model
from rcm.decision.ml.registry_models import ModelVersion
from rcm.decision.ml.serving_models import (
    DENIAL_RISK,
    RECOVERY,
    Deployment,
    Prediction,
    PredictionStatus,
    Status,
)
from rcm.platform.audit import DecisionType, record_decision
from rcm.platform.errors import ValidationFailedError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

#: How many SHAP drivers are kept with a prediction (SRS 9.1: "top drivers").
#: What is shown, not what is decided: not a threshold (FR-ML-07).
TOP_DRIVERS: Final = 5

NOT_DEPLOYED: Final = "no version is deployed"
IN_SHADOW: Final = "in shadow: scores are recorded, not used"
REVERTED: Final = "withdrawn; this work is on rules only"
NO_FEATURE_ROW: Final = "the warehouse holds no row for this claim yet"

ROUTE_REVIEW: Final = "REVIEW"
ROUTE_PASS: Final = "PASS"  # noqa: S105 -- a route, not a credential


@dataclass(frozen=True, slots=True)
class Spec:
    """What a served model reads, and when it decides."""

    features: tuple[Feature, ...]
    stage: Stage
    #: ML-3 scores a refusal; a line the payer did not refuse has nothing
    #: for it to say.
    denied_only: bool


SPECS: Final[Mapping[str, Spec]] = {
    DENIAL_RISK: Spec(DENIAL_RISK_FEATURES, Stage.SUBMISSION, denied_only=False),
    RECOVERY: Spec(RECOVERY_FEATURES, Stage.DENIAL, denied_only=True),
}


def spec(model_name: str) -> Spec:
    if model_name not in SPECS:
        raise ValidationFailedError(field="model", reason=f"{model_name} is not a served model")
    return SPECS[model_name]


@dataclass(frozen=True, slots=True)
class Served:
    """The ACTIVE version's scores by service line, or why there are none."""

    scores: Mapping[uuid.UUID, float] = field(default_factory=dict)
    #: The claim of every line the model was asked about.
    claim_of: Mapping[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    #: The line charge, for callers that weigh lines into a claim.
    charge_of: Mapping[uuid.UUID, float] = field(default_factory=dict)
    #: None when an ACTIVE version answered.
    unavailable: str | None = None


_models: dict[uuid.UUID, Model] = {}


def loaded(row: ModelVersion) -> Model:
    """The model a registry row holds, loaded once per process."""
    # A registry row is never rewritten, so a loaded model never goes stale.
    if row.model_version_id not in _models:
        _models[row.model_version_id] = registry.load(row)
    return _models[row.model_version_id]


def missing_features(model: Model, values: Sequence[object]) -> list[str]:
    """The features this row lacks that the model was never trained without."""
    return [
        f.name
        for f, value in zip(model.encoding.features, values, strict=True)
        if value is None and f.name not in OPTIONAL_FEATURES
    ]


def _route(model_name: str, score: float, config: thresholds.Thresholds) -> str | None:
    if model_name not in config.review_at:
        return None
    return ROUTE_REVIEW if score >= config.review_threshold(model_name) else ROUTE_PASS


def _record(
    session: Session,
    *,
    version: ModelVersion,
    mode: str,
    row: LineRow[None],
    score: float,
    drivers: Sequence[Driver],
    config: thresholds.Thresholds,
    now: datetime,
) -> Prediction:
    names = [f.name for f in loaded(version).encoding.features]
    output: dict[str, Any] = {
        "score": score,
        "drivers": [{"feature": d.feature, "contribution": d.contribution} for d in drivers],
        "mode": mode,
        # A shadow score is recorded and changes nothing (FR-ML-04).
        "used": mode == str(Status.ACTIVE),
        "thresholds_version": config.version,
    }
    route = _route(version.model_name, score, config)
    if route is not None:
        output["route"] = route
    decision = record_decision(
        session,
        entity_type="service_line",
        entity_id=row.service_line_id,
        decision_type=DecisionType.PREDICT,
        decided_by=f"model:{version.model_name}",
        logic_version=f"{version.model_name} v{version.version}",
        input_snapshot={"features": dict(zip(names, row.values, strict=True))},
        output=output,
        confidence=score,
    )
    session.flush()
    prediction = Prediction(
        model_version_id=version.model_version_id,
        model_name=version.model_name,
        version=version.version,
        mode=mode,
        client_id=row.client_id,
        claim_id=row.claim_id,
        service_line_id=row.service_line_id,
        status=str(PredictionStatus.SCORED),
        score=score,
        decision_id=decision.decision_id,
        predicted_at=now,
    )
    session.add(prediction)
    return prediction


def _degraded(
    session: Session,
    *,
    version: ModelVersion,
    mode: str,
    client_id: uuid.UUID,
    claim_id: uuid.UUID,
    service_line_id: uuid.UUID | None,
    reason: str,
    now: datetime,
) -> None:
    session.add(
        Prediction(
            model_version_id=version.model_version_id,
            model_name=version.model_name,
            version=version.version,
            mode=mode,
            client_id=client_id,
            claim_id=claim_id,
            service_line_id=service_line_id,
            status=str(PredictionStatus.DEGRADED),
            reason=reason,
            predicted_at=now,
        )
    )


def score_rows(
    session: Session,
    *,
    deployed: Deployment,
    rows: Sequence[LineRow[None]],
    now: datetime,
) -> dict[uuid.UUID, float]:
    """Score these assembled rows with one serving version, recording each.

    Rows already scored by this version are read back, not scored again.
    """
    if deployed.status not in deployment.SERVING:
        raise ValidationFailedError(
            field="version",
            reason=f"{deployed.model_name} v{deployed.version} is {deployed.status}",
        )
    version = deployment.accepted_version(session, deployed.model_name, deployed.version)
    model = loaded(version)
    config = thresholds.load()
    known = {
        p.service_line_id: p
        for p in session.scalars(
            select(Prediction).where(
                Prediction.model_version_id == version.model_version_id,
                Prediction.service_line_id.in_([r.service_line_id for r in rows]),
            )
        )
    }
    fresh: list[LineRow[None]] = []
    degraded = 0
    for row in rows:
        if row.service_line_id in known:
            continue
        missing = missing_features(model, row.values)
        if missing:
            degraded += 1
            _degraded(
                session,
                version=version,
                mode=deployed.status,
                client_id=row.client_id,
                claim_id=row.claim_id,
                service_line_id=row.service_line_id,
                reason="missing: " + ", ".join(missing),
                now=now,
            )
        else:
            fresh.append(row)
    scores = {
        line: p.score for line, p in known.items() if line is not None and p.score is not None
    }
    if fresh:
        inputs = [r.values for r in fresh]
        predicted = model.predict(inputs).tolist()
        for row, value, drivers in zip(
            fresh, predicted, model.drivers(inputs, top=TOP_DRIVERS), strict=True
        ):
            _record(
                session,
                version=version,
                mode=deployed.status,
                row=row,
                score=float(value),
                drivers=drivers,
                config=config,
                now=now,
            )
            scores[row.service_line_id] = float(value)
    if degraded:
        # Feature names are column names, not values: nothing here is PHI.
        log.warning(
            "ml.degraded",
            model=version.model_name,
            version=version.version,
            lines=degraded,
        )
    session.flush()
    return scores


def _no_row(
    session: Session,
    *,
    deployed: Deployment,
    client_id: uuid.UUID,
    claim_ids: Sequence[uuid.UUID],
    now: datetime,
) -> None:
    """Record, once, each claim the warehouse cannot describe yet."""
    version = deployment.accepted_version(session, deployed.model_name, deployed.version)
    already = set(
        session.scalars(
            select(Prediction.claim_id).where(
                Prediction.model_version_id == version.model_version_id,
                Prediction.claim_id.in_(claim_ids),
                Prediction.service_line_id.is_(None),
            )
        )
    )
    for claim_id in claim_ids:
        if claim_id not in already:
            _degraded(
                session,
                version=version,
                mode=deployed.status,
                client_id=client_id,
                claim_id=claim_id,
                service_line_id=None,
                reason=NO_FEATURE_ROW,
                now=now,
            )


def score(
    session: Session,
    model_name: str,
    *,
    client_id: uuid.UUID,
    claim_ids: Sequence[uuid.UUID],
    now: datetime,
) -> Served:
    """Every serving version scores these claims' lines; the ACTIVE one's
    scores come back."""
    what = spec(model_name)
    states = deployment.states(session, model_name)
    serving = [d for d in states.values() if d.status in deployment.SERVING]
    if not serving:
        last = deployment.latest(session, model_name)
        withdrawn = last is not None and last.status == str(Status.REVERTED)
        return Served(unavailable=REVERTED if withdrawn else NOT_DEPLOYED)
    if not claim_ids:
        return Served()
    rows = [
        r
        for r in serving_lines(
            session,
            features=what.features,
            stage=what.stage,
            claim_ids=claim_ids,
            denied_only=what.denied_only,
        ).rows
        # Query-layer scoping (PD-20): another client's line is never scored
        # for this one, whatever ids were asked for.
        if r.client_id == client_id
    ]
    described = claims_in_warehouse(session, claim_ids)
    absent = [c for c in dict.fromkeys(claim_ids) if c not in described]
    active: dict[uuid.UUID, float] | None = None
    for deployed in serving:
        scores = score_rows(session, deployed=deployed, rows=rows, now=now)
        if absent:
            _no_row(session, deployed=deployed, client_id=client_id, claim_ids=absent, now=now)
        if deployed.status == str(Status.ACTIVE):
            active = scores
    session.flush()
    charge = [f.name for f in what.features].index("line_charge")
    return Served(
        scores=active or {},
        claim_of={r.service_line_id: r.claim_id for r in rows},
        charge_of={r.service_line_id: float(r.values[charge] or 0.0) for r in rows},
        unavailable=None if active is not None else IN_SHADOW,
    )
