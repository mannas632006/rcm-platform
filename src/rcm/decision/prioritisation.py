"""Queue prioritisation: the heuristic, before any model (M3.6, SRS 7.8).

    PD-15: Timely-filing deadline overrides all other prioritisation.
    BR-GLOBAL-04 (MUST): Any claim within `[TBD-37]` days of its deadline
    MUST be escalated to a human queue with highest priority, regardless of
    its other attributes.
    FR-WORK-05 (MUST): Deadline-pinned items bypass the scoring model
    entirely.

The order of operations *is* the rule. The deadline is checked first, and a
pinned item returns before `_score` is ever called -- so no score, today's
heuristic or M6's model, has a code path by which it could demote a claim
about to expire. "Score everything, then override the urgent ones" would
satisfy the test today and leave that path open tomorrow.

An unknown deadline pins too (owner decision 2026-11-01, PD-18). Reading a
missing filing term as "far away" is how a claim quietly expires.

Otherwise SRS 7.8's shape: expected value (balance x P(recovery)) divided by
estimated effort, with age decay and a deadline-urgency multiplier, squashed
onto 0-99. 100 is reserved for pinned items. The constants below are a
heuristic, not a measurement -- they are versioned with the logic so every
recorded decision says which numbers produced it (PD-1), and M6 replaces
P(recovery) with a learned model behind the same deadline gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from rcm.platform.errors import ValidationFailedError

#: Bump on any change to the constants or the arithmetic below. Recorded on
#: every decision so a past ranking can be reproduced exactly.
LOGIC_VERSION: Final = "prioritisation-heuristic-v1"

#: P(recovery) by work kind. Heuristic priors until M6 learns them from
#: CORE outcomes. Keyed by kind name: `rcm.decision` sits below `rcm.work`
#: and cannot import its enum.
P_RECOVERY: Final[dict[str, float]] = {
    "SCRUB_EXCEPTION": 0.95,
    "SUBMISSION_REJECTION": 0.90,
    "PAYMENT_EXCEPTION": 0.70,
    "ELIGIBILITY_PROBLEM": 0.60,
    "DENIAL": 0.50,
    "AUTH_PROBLEM": 0.40,
    "PAYER_MATCH_REVIEW": 0.50,
    "DATA_QUALITY": 0.50,
    "RULE_CONFLICT": 0.50,
    "MODEL_LOW_CONFIDENCE": 0.50,
    "EXTERNAL_SYSTEM_FAILURE": 0.50,
    "COMPLIANCE_CONCERN": 0.50,
    "DEADLINE_RISK": 0.50,
    "MISSING_FILING_DEADLINE": 0.50,
}

#: Estimated hours of human effort per kind. A rejection is a field fixed and
#: resent; a denial is usually an appeal.
EFFORT_HOURS: Final[dict[str, float]] = {
    "SCRUB_EXCEPTION": 0.25,
    "SUBMISSION_REJECTION": 0.25,
    "PAYMENT_EXCEPTION": 0.5,
    "ELIGIBILITY_PROBLEM": 0.5,
    "DENIAL": 1.0,
    "AUTH_PROBLEM": 1.0,
    "PAYER_MATCH_REVIEW": 0.25,
    "DATA_QUALITY": 0.5,
    "RULE_CONFLICT": 1.0,
    "MODEL_LOW_CONFIDENCE": 0.5,
    "EXTERNAL_SYSTEM_FAILURE": 0.5,
    "COMPLIANCE_CONCERN": 1.0,
    "DEADLINE_RISK": 0.5,
    "MISSING_FILING_DEADLINE": 0.25,
}

#: Recovery odds halve every this many days an item has waited.
AGE_HALF_LIFE_DAYS: Final = 90.0
#: Inside this many days of the deadline the urgency multiplier rises from 1
#: towards 2 (DOC-02: "escalates sharply as timely filing approaches").
URGENCY_WINDOW_DAYS: Final = 120.0
#: Value-per-effort-hour at which the score reaches half the scale.
HALF_SCALE_VALUE: Final = 200.0

_PINNED_SCORE: Final = 100
_TOP_SCORE: Final = 99


@dataclass(frozen=True, slots=True)
class PriorityInputs:
    """What the decision is made from -- recorded verbatim (PD-1)."""

    kind: str
    balance: Decimal
    #: None when the deadline cannot be computed: an unknown filing term or
    #: an unknown date of service.
    days_to_deadline: int | None
    age_days: int
    has_claim: bool


@dataclass(frozen=True, slots=True)
class PriorityResult:
    score: int
    pinned: bool
    #: FR-WORK-06: why this item ranks where it does, in words.
    reason: str
    #: FR-WORK-06: the arithmetic behind the score.
    components: dict[str, str] = field(default_factory=dict)
    #: BR-M10-06: below the individual-work threshold.
    bulk: bool = False
    logic_version: str = LOGIC_VERSION


def prioritise(
    inputs: PriorityInputs, *, escalation_days: int, bulk_threshold: Decimal
) -> PriorityResult:
    """Pin, or score. The deadline is decided before anything else runs."""
    if inputs.has_claim:
        if inputs.days_to_deadline is None:
            return PriorityResult(
                score=_PINNED_SCORE,
                pinned=True,
                reason="filing deadline unknown: treated as urgent until it is entered",
            )
        if inputs.days_to_deadline <= escalation_days:
            return PriorityResult(
                score=_PINNED_SCORE,
                pinned=True,
                reason=f"filing deadline in {inputs.days_to_deadline} days "
                f"(escalation threshold {escalation_days})",
            )

    score, components = _score(inputs)
    expected = Decimal(components["expected_value"])
    bulk = inputs.has_claim and expected < bulk_threshold
    reason = (
        f"expected recovery {components['expected_value']} over "
        f"{components['effort_hours']}h of work"
        + (" -- below the individual-work threshold, routed to bulk" if bulk else "")
    )
    return PriorityResult(
        score=score, pinned=False, reason=reason, components=components, bulk=bulk
    )


def _score(inputs: PriorityInputs) -> tuple[int, dict[str, str]]:
    """SRS 7.8's expected-value score on 0-99. Never called for a pinned item."""
    if inputs.kind not in P_RECOVERY or inputs.kind not in EFFORT_HOURS:
        # PD-18: a kind with no prior is a configuration gap, not a zero.
        raise ValidationFailedError(field="kind", reason=f"no priority prior for {inputs.kind}")
    if inputs.balance < 0 or inputs.age_days < 0:
        raise ValidationFailedError(field="inputs", reason="balance and age are never negative")

    p_recovery = P_RECOVERY[inputs.kind]
    effort = EFFORT_HOURS[inputs.kind]
    expected = (inputs.balance * Decimal(str(p_recovery))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    decay = 0.5 ** (inputs.age_days / AGE_HALF_LIFE_DAYS)
    urgency = 1.0
    if inputs.days_to_deadline is not None:
        urgency += max(0.0, URGENCY_WINDOW_DAYS - inputs.days_to_deadline) / URGENCY_WINDOW_DAYS
        urgency = min(urgency, 2.0)

    raw = float(expected) / effort * decay * urgency
    score = min(_TOP_SCORE, math.floor(_TOP_SCORE * raw / (raw + HALF_SCALE_VALUE)))

    return score, {
        "expected_value": str(expected),
        "p_recovery": f"{p_recovery:.2f}",
        "effort_hours": f"{effort:g}",
        "age_decay": f"{decay:.3f}",
        "urgency": f"{urgency:.3f}",
    }
