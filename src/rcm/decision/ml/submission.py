"""What ML-1 may add to a scrub, and what it may never take away (PD-5).

    PD-5: Deterministic rules always override model output on conflict.

The rules' verdict stands. A model that thinks a blocked claim is fine does
not unblock it, and a model that dislikes a claim the rules passed does not
block it: it can only ask that a person look before it is sent. So the one
thing ML-1 can change is PASS into ADVISE, and only when its score reaches
the review threshold in `thresholds.toml`.

`denial_risk` is the scrub's one call into the model. Like the queues' seam
(ADR-0020), it never stops the scrub: any failure is logged by type and the
claim goes on with the rules' verdict alone (FR-ML-06).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from rcm.decision.ml import serving, thresholds
from rcm.decision.ml.serving_models import DENIAL_RISK
from rcm.decision.rules.evaluate import Verdict
from rcm.platform.logging import get_logger

log = get_logger(__name__)

FAILED = "the model did not answer"
NOT_ALL_LINES = "not every line could be scored"


@dataclass(frozen=True, slots=True)
class Risk:
    """ML-1's P(denial) for a claim -- its riskiest line -- or why none."""

    score: float | None
    unavailable: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"model": DENIAL_RISK, "score": self.score, "unavailable": self.unavailable}


def denial_risk(
    session: Session, *, client_id: uuid.UUID, claim_id: uuid.UUID, now: datetime
) -> Risk:
    """Score the claim's lines as submitted with the serving ML-1 versions.

    The claim need not be in the warehouse: its inputs are computed from
    CORE as the warehouse will compute them (ADR-0032).

    A claim is as risky as its riskiest line. A claim with any line the model
    could not score has no score: the line it could not see may be the one
    that is refused.
    """
    try:
        with session.begin_nested():
            served = serving.score_submission(
                session, DENIAL_RISK, client_id=client_id, claim_ids=[claim_id], now=now
            )
    except Exception as exc:  # the model boundary: any failure degrades
        log.error("ml.denial_risk_unavailable", error=type(exc).__name__)
        return Risk(score=None, unavailable=FAILED)
    if served.unavailable is not None:
        return Risk(score=None, unavailable=served.unavailable)
    lines = [line for line, claim in served.claim_of.items() if claim == claim_id]
    if not lines:
        return Risk(score=None, unavailable=serving.NO_LINES)
    if any(line not in served.scores for line in lines):
        return Risk(score=None, unavailable=NOT_ALL_LINES)
    return Risk(score=max(served.scores[line] for line in lines), unavailable=None)


def combine(rules: Verdict, denial_risk: float | None) -> Verdict:
    """The verdict on a claim, given the rules' and ML-1's P(denial).

    `denial_risk` is None when the model gave no score: shadow, withdrawn,
    or a line it could not score. The rules' verdict is then the verdict.
    """
    if rules is not Verdict.PASS or denial_risk is None:
        return rules
    review_at = thresholds.load().review_threshold(DENIAL_RISK)
    return Verdict.ADVISE if denial_risk >= review_at else Verdict.PASS
