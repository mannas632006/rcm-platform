"""The shadow harness: score a window, then compare with what happened (M6.6).

    FR-ML-04 (MUST): No model reaches production without a shadow period.
    M6.6: "Model output recorded, not used, comparison reported."

`run` scores every claim submitted in a window with whatever versions are
serving. A version in SHADOW is recorded and returns nothing to anyone, which
is `serving.score`'s rule, not this module's.

`compare` reads a version's recorded scores back against the outcomes that
have since matured, beside what it would replace: the ACTIVE version on the
same lines when there is one, and the baseline it was accepted against.

A training-time module: it imports scikit-learn through `evaluate`. Nothing
a request reaches imports it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.analytics.dataset import LineRow, claims_submitted
from rcm.decision.ml import deployment, monitoring, serving
from rcm.decision.ml.evaluate import binary_report
from rcm.decision.ml.pipeline import recovery_prior, rules_flags
from rcm.decision.ml.serving_models import (
    DENIAL_RISK,
    RECOVERY,
    Prediction,
    PredictionStatus,
    Status,
)

_CHUNK: Final = 200


def run(
    session: Session,
    *,
    model_name: str,
    client_id: uuid.UUID,
    since: date,
    until: date,
    now: datetime,
) -> int:
    """Score the claims submitted in `[since, until)`. Returns how many."""
    claims = claims_submitted(session, since=since, until=until)
    for start in range(0, len(claims), _CHUNK):
        serving.score(
            session,
            model_name,
            client_id=client_id,
            claim_ids=claims[start : start + _CHUNK],
            now=now,
        )
    return len(claims)


@dataclass(frozen=True, slots=True)
class _Scores:
    """A version's recorded scores: by the 835's line, and by the line as
    submitted for those made before an 835 existed (ADR-0032)."""

    by_service_line: dict[uuid.UUID, float]
    by_claim_line: dict[uuid.UUID, float]

    def __len__(self) -> int:
        return len(self.by_service_line) + len(self.by_claim_line)

    def of(self, row: LineRow[bool]) -> float | None:
        if row.service_line_id in self.by_service_line:
            return self.by_service_line[row.service_line_id]
        if row.claim_line_id is not None:
            return self.by_claim_line.get(row.claim_line_id)
        return None


def _scores(session: Session, model_name: str, version: int) -> _Scores:
    found = _Scores({}, {})
    for service_line, claim_line, score in session.execute(
        select(Prediction.service_line_id, Prediction.claim_line_id, Prediction.score).where(
            Prediction.model_name == model_name,
            Prediction.version == version,
            Prediction.status == str(PredictionStatus.SCORED),
        )
    ):
        if score is None:
            continue
        if service_line is not None:
            found.by_service_line[service_line] = score
        elif claim_line is not None:
            found.by_claim_line[claim_line] = score
    return found


def compare(session: Session, *, model_name: str, version: int, today: date) -> dict[str, Any]:
    """A version's recorded scores against matured outcomes and its rivals."""
    scores = _scores(session, model_name, version)
    requests = session.scalars(
        select(Prediction.status).where(
            Prediction.model_name == model_name, Prediction.version == version
        )
    ).all()
    ours: dict[uuid.UUID, float] = {}
    matured: list[LineRow[bool]] = []
    for row in monitoring.TRAINING[model_name](session).rows:
        score = scores.of(row)
        if score is not None:
            matured.append(row)
            ours[row.service_line_id] = score
    report: dict[str, Any] = {
        "model": model_name,
        "version": version,
        "requests": len(requests),
        "scored": len(scores),
        "degraded": sum(1 for s in requests if s == str(PredictionStatus.DEGRADED)),
        "matured": len(matured),
        "rivals": {},
    }
    if not matured:
        return report
    truth = [r.label for r in matured]
    payers = [r.payer_id for r in matured]
    report["model_metrics"] = binary_report(
        truth, [ours[r.service_line_id] for r in matured], payers=payers
    )
    if model_name == RECOVERY:
        report["rivals"]["queue prior"] = binary_report(
            truth, recovery_prior(matured), payers=payers
        )
    elif model_name == DENIAL_RISK:
        report["rivals"]["rules baseline"] = binary_report(
            truth, rules_flags(session, matured, today=today), payers=payers
        )
    for other, state in deployment.states(session, model_name).items():
        if other != version and state.status == str(Status.ACTIVE):
            rival = _scores(session, model_name, other)
            theirs = {
                r.service_line_id: score for r in matured if (score := rival.of(r)) is not None
            }
            both = [r for r in matured if r.service_line_id in theirs]
            if both:
                # Like for like: the same lines, each version's own score.
                report["rivals"][f"v{other} (active)"] = {
                    "lines": len(both),
                    "theirs": binary_report(
                        [r.label for r in both],
                        [theirs[r.service_line_id] for r in both],
                        payers=[r.payer_id for r in both],
                    ),
                    "ours": binary_report(
                        [r.label for r in both],
                        [ours[r.service_line_id] for r in both],
                        payers=[r.payer_id for r in both],
                    ),
                }
    return report
