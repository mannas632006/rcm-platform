"""Model scores, and what the screens get when the model cannot give them (M5.12).

    UR-34 (MUST): The PARTIAL state is mandatory wherever a screen combines
    core data with model output. Model degradation must never prevent an
    operator from working a claim (mirrors FR-ML-06).

ML-3's P(recovery) arrives with M6. This module is the one seam the work
queues read it through, so the degraded path exists, and is tested, before
the model does:

- **No model deployed** (today): every score is unavailable, with that reason.
- **The model fails** -- raises, times out, or answers outside [0, 1]: the
  failure is logged by its type (never its message, which may carry inputs:
  PD-13), and the scores are unavailable with a reason a person can read.
  Nothing is guessed (FR-ML-06), and nothing else on the screen waits on it.
- A score that is missing, or not a probability, is unavailable for that
  claim only. It is never shown as zero.

The model runs inside a savepoint, so a database error in it cannot poison
the read it is part of.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from sqlalchemy.orm import Session

from rcm.platform.logging import get_logger

log = get_logger(__name__)

NOT_DEPLOYED: Final = "No recovery model is deployed yet (M6)."
FAILED: Final = "The recovery model did not answer."


class RecoveryModel(Protocol):
    """ML-3, as M6 will deploy it."""

    def score(
        self, session: Session, *, client_id: uuid.UUID, claim_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, float]: ...


@dataclass(frozen=True, slots=True)
class Scores:
    """P(recovery) by claim, or why there are none."""

    values: Mapping[uuid.UUID, float] = field(default_factory=dict)
    #: None when the model answered; otherwise why it did not.
    unavailable: str | None = None


_deployed: RecoveryModel | None = None


def deploy_recovery_model(model: RecoveryModel | None) -> None:
    """Install the model the queues read (M6), or None to read none."""
    # One process-wide deployment, by design.
    global _deployed
    _deployed = model


def deployed() -> bool:
    return _deployed is not None


def _probability(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def recovery_scores(
    session: Session, *, client_id: uuid.UUID, claim_ids: Sequence[uuid.UUID]
) -> Scores:
    """The model's scores for these claims, or unavailable -- never an error
    that blocks the caller, and never a guess."""
    model = _deployed
    if model is None:
        return Scores(unavailable=NOT_DEPLOYED)
    if not claim_ids:
        return Scores()
    wanted = set(claim_ids)
    try:
        with session.begin_nested():
            answered = model.score(session, client_id=client_id, claim_ids=claim_ids)
        # Read inside the boundary: an answer that is not a mapping is a
        # failed model too, not a crash of the screen around it.
        pairs = list(answered.items())
    except Exception as exc:  # the model boundary: any failure degrades
        # Logged by type only: a model's error text may echo its inputs (PD-13).
        log.error("scoring.recovery_unavailable", error=type(exc).__name__)
        return Scores(unavailable=FAILED)
    values = {
        claim: score
        for claim, raw in pairs
        if claim in wanted and (score := _probability(raw)) is not None
    }
    if len(values) < len(pairs):
        log.warning("scoring.recovery_dropped", dropped=len(pairs) - len(values))
    return Scores(values=values)
