"""What ML-1 may add to a scrub, and what it may never take away (PD-5).

    PD-5: Deterministic rules always override model output on conflict.

The rules' verdict stands. A model that thinks a blocked claim is fine does
not unblock it, and a model that dislikes a claim the rules passed does not
block it: it can only ask that a person look before it is sent. So the one
thing ML-1 can change is PASS into ADVISE, and only when its score reaches
the review threshold in `thresholds.toml`.
"""

from __future__ import annotations

from rcm.decision.ml import thresholds
from rcm.decision.ml.serving_models import DENIAL_RISK
from rcm.decision.rules.evaluate import Verdict


def combine(rules: Verdict, denial_risk: float | None) -> Verdict:
    """The verdict on a claim, given the rules' and ML-1's P(denial).

    `denial_risk` is None when the model gave no score: shadow, withdrawn,
    or a line it could not score. The rules' verdict is then the verdict.
    """
    if rules is not Verdict.PASS or denial_risk is None:
        return rules
    review_at = thresholds.load().review_threshold(DENIAL_RISK)
    return Verdict.ADVISE if denial_risk >= review_at else Verdict.PASS
