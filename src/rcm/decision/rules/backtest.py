"""Backtest one rule version against one client's history (M4.4, ADR-0009).

BR-M7-04: "Every rule MUST be testable against historical claims before
deployment, with its false-positive rate measured."

Each claim with a known outcome is scored:

- flagged and denied: a true positive;
- flagged and paid: a false positive, a good claim the rule would have
  stopped;
- not flagged and denied: a missed denial;
- INDETERMINATE: neither; counted apart.

FP rate = false positives / flagged. A claim with no 835 yet has no outcome
and is counted apart, never as paid.

The rule runs through `evaluate`, the code that scrubs live claims, as a
shadow: a draft is backtested before it can act on anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.analytics.denials import denied_submitter_claim_ids
from rcm.decision.rules.definition import RuleStatus, parse_rule
from rcm.decision.rules.evaluate import ActiveRule, Evaluation, Outcome, evaluate
from rcm.decision.rules.models import RuleBacktest, RuleVersion
from rcm.decision.rules.scrub import client_facts
from rcm.domain.remittance import ClaimPayment
from rcm.platform.errors import ResourceNotFoundError, ValidationFailedError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

_RATE_PLACES: Final = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class Tally:
    evaluated: int = 0
    flagged: int = 0
    true_positives: int = 0
    false_positives: int = 0
    missed_denials: int = 0
    indeterminate: int = 0
    without_outcome: int = 0

    @property
    def fp_rate(self) -> Decimal | None:
        """None when nothing was flagged: a rule never seen stopping a claim
        has shown nothing about its accuracy."""
        if not self.flagged:
            return None
        return (Decimal(self.false_positives) / Decimal(self.flagged)).quantize(_RATE_PLACES)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    backtest_id: uuid.UUID
    rule_id: str
    version: int
    tally: Tally


def tally(results: Iterable[tuple[Outcome | None, bool | None]]) -> Tally:
    """Count (what the rule said, whether the claim was denied) pairs.

    The rule's word is None when it passed the claim; denied is None when no
    outcome is on file.
    """
    counts = dict.fromkeys(Tally.__dataclass_fields__, 0)
    for said, denied in results:
        if denied is None:
            counts["without_outcome"] += 1
            continue
        counts["evaluated"] += 1
        if said is Outcome.FLAGGED:
            counts["flagged"] += 1
            counts["true_positives" if denied else "false_positives"] += 1
        elif said is Outcome.INDETERMINATE:
            counts["indeterminate"] += 1
        elif denied:
            counts["missed_denials"] += 1
    return Tally(**counts)


def _said(evaluation: Evaluation) -> Outcome | None:
    outcomes = {finding.outcome for finding in evaluation.findings}
    if Outcome.FLAGGED in outcomes:
        return Outcome.FLAGGED
    if Outcome.INDETERMINATE in outcomes:
        return Outcome.INDETERMINATE
    return None


def run_backtest(
    session: Session, *, rule_id: str, version: int, client_id: uuid.UUID, ran_by: str
) -> BacktestResult:
    """Score the rule version on the client's claims, and keep the result."""
    if not ran_by.strip():
        raise ValidationFailedError(field="ran_by", reason="name who ran it")
    row = session.get(RuleVersion, (rule_id, version))
    if row is None:
        raise ResourceNotFoundError(resource="rule_version")
    # A shadow: the backtest decides nothing about any claim.
    candidate = ActiveRule(rule=parse_rule(row.document), version=version, status=RuleStatus.SHADOW)

    denied = set(session.scalars(denied_submitter_claim_ids(client_id)))
    # An outcome is known once a posting that is not a reversal is on file.
    adjudicated = set(
        session.scalars(
            select(ClaimPayment.claim_id)
            .distinct()
            .where(ClaimPayment.client_id == client_id, ClaimPayment.is_reversal.is_(False))
        )
    )

    def scored() -> Iterable[tuple[Outcome | None, bool | None]]:
        for claim, facts in client_facts(session, client_id=client_id):
            known = claim.submitter_claim_id in adjudicated
            outcome = claim.submitter_claim_id in denied if known else None
            yield _said(evaluate([candidate], facts)), outcome

    result = tally(scored())
    record = RuleBacktest(
        rule_id=rule_id,
        version=version,
        client_id=client_id,
        claims_evaluated=result.evaluated,
        flagged=result.flagged,
        true_positives=result.true_positives,
        false_positives=result.false_positives,
        missed_denials=result.missed_denials,
        indeterminate=result.indeterminate,
        without_outcome=result.without_outcome,
        fp_rate=result.fp_rate,
        ran_by=ran_by,
    )
    session.add(record)
    session.flush()
    log.info(
        "rule.backtested",
        rule_id=rule_id,
        version=version,
        evaluated=result.evaluated,
        flagged=result.flagged,
        fp_rate=str(result.fp_rate),
    )
    return BacktestResult(record.backtest_id, rule_id, version, result)
