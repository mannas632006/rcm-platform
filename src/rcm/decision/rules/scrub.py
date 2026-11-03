"""Scrub one claim against the latest ruleset, and record it (M4.2).

FR-RULES-01 / BR-M7-02: every evaluated claim records the ruleset version
that evaluated it. One `decision_record` per evaluation (PD-1), with the
facts, the ruleset's version and digest, and every finding. The facts hold
no PHI (ADR-0007 Decision 4), so they are recorded in full.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.decision import ncci_store
from rcm.decision.rules.evaluate import ClaimFacts, Evaluation, LineFacts, evaluate
from rcm.decision.rules.store import load_ruleset
from rcm.domain.claim import Claim, ClaimLine
from rcm.platform.audit import DecisionType, record_decision
from rcm.platform.errors import ResourceNotFoundError

DECIDED_BY: Final = "rules_engine:scrubber"


@dataclass(frozen=True, slots=True)
class ScrubResult:
    ruleset_version: int
    ruleset_digest: str
    evaluation: Evaluation


def claim_facts(session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID) -> ClaimFacts:
    """Every field a rule may name, for one claim of one client (PD-20)."""
    claim = session.scalar(
        select(Claim).where(Claim.claim_id == claim_id, Claim.client_id == client_id)
    )
    if claim is None:
        raise ResourceNotFoundError(resource="claim")
    lines = session.scalars(
        select(ClaimLine)
        .where(ClaimLine.client_id == client_id, ClaimLine.claim_id == claim_id)
        .order_by(ClaimLine.line_number)
    ).all()
    return facts_from(claim, lines)


def client_facts(session: Session, *, client_id: uuid.UUID) -> Iterator[tuple[Claim, ClaimFacts]]:
    """Every claim of one client with its facts, in two queries (M4.4)."""
    lines: dict[uuid.UUID, list[ClaimLine]] = defaultdict(list)
    for line in session.scalars(
        select(ClaimLine)
        .where(ClaimLine.client_id == client_id)
        .order_by(ClaimLine.claim_id, ClaimLine.line_number)
    ):
        lines[line.claim_id].append(line)
    for claim in session.scalars(
        select(Claim).where(Claim.client_id == client_id).order_by(Claim.claim_id)
    ):
        yield claim, facts_from(claim, lines[claim.claim_id])


def facts_from(claim: Claim, lines: Sequence[ClaimLine]) -> ClaimFacts:
    """The one place a claim becomes facts: scrub and backtest share it."""
    return ClaimFacts(
        claim={
            "claim.payer_id": claim.payer_id,
            "claim.claim_frequency_code": claim.claim_frequency_code,
            "claim.claim_filing_code": claim.claim_filing_code,
            "claim.place_of_service": claim.place_of_service,
            "claim.total_charge": claim.total_charge,
            "claim.date_of_service": claim.date_of_service,
            "claim.has_prior_auth": bool(claim.prior_auth_number),
            "claim.diagnoses": list(claim.diagnoses),
            "claim.procedure_codes": [line.procedure_code for line in lines],
            "claim.line_count": len(lines),
        },
        lines=tuple(
            LineFacts(
                number=line.line_number,
                values={
                    "line.procedure_code": line.procedure_code,
                    "line.modifiers": [
                        m
                        for m in (
                            line.modifier_1,
                            line.modifier_2,
                            line.modifier_3,
                            line.modifier_4,
                        )
                        if m
                    ],
                    "line.units": line.units,
                    "line.charge_amount": line.charge_amount,
                    "line.service_date": line.service_date,
                },
            )
            for line in lines
        ),
    )


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def scrub_claim(session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID) -> ScrubResult:
    """Evaluate the claim and record the decision in the caller's transaction."""
    facts = claim_facts(session, claim_id=claim_id, client_id=client_id)
    ruleset = load_ruleset(session)
    # Exactly the NCCI releases the ruleset pinned (ADR-0008 Decision 3).
    ncci = ncci_store.check_for(
        session,
        ptp_release=ruleset.ncci_ptp_release,
        mue_release=ruleset.ncci_mue_release,
        codes={str(line.values["line.procedure_code"]) for line in facts.lines},
    )
    evaluation = evaluate(ruleset.rules, facts, checks=[ncci])
    record_decision(
        session,
        entity_type="claim",
        entity_id=claim_id,
        decision_type=DecisionType.SCRUB,
        decided_by=DECIDED_BY,
        logic_version=f"ruleset-v{ruleset.version}",
        input_snapshot={
            "ruleset_version": ruleset.version,
            "ruleset_digest": ruleset.digest,
            "ncci_ptp_release": ruleset.ncci_ptp_release,
            "ncci_mue_release": ruleset.ncci_mue_release,
            "claim": {k: _plain(v) for k, v in facts.claim.items()},
            "lines": [
                {"line_number": line.number, **{k: _plain(v) for k, v in line.values.items()}}
                for line in facts.lines
            ],
        },
        output={
            "verdict": str(evaluation.verdict),
            "layers_run": [str(layer) for layer in evaluation.layers_run],
            "findings": [
                {
                    "rule_id": f.rule_id,
                    "rule_version": f.rule_version,
                    "layer": str(f.layer),
                    "mode": str(f.mode),
                    "outcome": str(f.outcome),
                    "line_number": f.line_number,
                    "route_to": f.route_to,
                    "reference": f.reference,
                }
                for f in evaluation.findings
            ],
        },
    )
    session.flush()
    return ScrubResult(
        ruleset_version=ruleset.version, ruleset_digest=ruleset.digest, evaluation=evaluation
    )
