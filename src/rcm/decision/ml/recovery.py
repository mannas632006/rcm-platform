"""ML-3 behind the queues' scoring seam (M6.3, ADR-0020, ADR-0028).

`rcm.decision.scoring` is the one way a queue reads P(recovery). This is the
model it reads: the deployed ML-3, scored and recorded through
`serving.score`.

ML-3 scores a denied *line*; a queue row is a *claim*. A claim's score is its
lines' scores weighed by what each line was charged: the share of the denied
money the model expects back. A claim with any denied line that could not be
scored has no score at all. Half an answer shown as a whole one is a guess
(FR-ML-06).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.orm import Session

from rcm.decision import scoring
from rcm.decision.ml import serving
from rcm.decision.ml.serving_models import RECOVERY

IN_SHADOW: Final = "The recovery model is in shadow: its scores are recorded, not shown."
REVERTED: Final = "The recovery model was withdrawn after drift."

_REASONS: Final[Mapping[str, str]] = {
    serving.NOT_DEPLOYED: scoring.NOT_DEPLOYED,
    serving.IN_SHADOW: IN_SHADOW,
    serving.REVERTED: REVERTED,
}


def claim_scores(served: serving.Served) -> dict[uuid.UUID, float]:
    """Each claim's expected share of its denied charges recovered."""
    lines: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for line, claim in served.claim_of.items():
        lines[claim].append(line)
    out: dict[uuid.UUID, float] = {}
    for claim, members in lines.items():
        if any(line not in served.scores for line in members):
            continue
        charged = sum(served.charge_of[line] for line in members)
        if charged > 0:
            out[claim] = (
                sum(served.scores[line] * served.charge_of[line] for line in members) / charged
            )
        else:
            out[claim] = sum(served.scores[line] for line in members) / len(members)
    return out


class ServedRecovery:
    """`scoring.RecoveryModel`, answered by whichever ML-3 version is ACTIVE."""

    def score(
        self, session: Session, *, client_id: uuid.UUID, claim_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, float]:
        served = serving.score(
            session, RECOVERY, client_id=client_id, claim_ids=claim_ids, now=datetime.now(UTC)
        )
        if served.unavailable is not None:
            raise scoring.Unavailable(_REASONS[served.unavailable])
        return claim_scores(served)
