"""Scrubber exceptions: what the rules find on claims already sent (M5.8, ADR-0019).

The practice management system sends the claims this platform loads
(ADR-0006), so by the time the scrubber sees one, it is with the payer. A
blocking finding is still worth a person's time before the 835 arrives: the
claim can be corrected and replaced instead of waiting to be denied for a
reason the rules already know.

**Review, never correct.** The scrub runs with `apply_corrections=False`. A
fix applied to a sent claim would make the stored row disagree with the 837
the payer holds, which is the one thing the row must never do (PD-9, PD-10).

**One named item per blocking finding** (BR-EXC-01): the rule, its version,
the line, and the rule's own message, routed where the rule says
(`route_to`). Advisory and shadow findings are recorded in the scrub's
`decision_record` but raise nothing.

**Once per claim per ruleset.** A claim already reviewed under the latest
ruleset is skipped, so a second run raises nothing and records nothing; a new
ruleset reviews every waiting claim again.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.decision.rules.evaluate import Finding, Mode
from rcm.decision.rules.scrub import scrub_claim
from rcm.decision.rules.store import LoadedRuleset, load_ruleset
from rcm.domain.claim import Claim
from rcm.domain.claim_state import ClaimState
from rcm.platform.audit import DecisionRecord, DecisionType
from rcm.platform.errors import ValidationFailedError
from rcm.platform.logging import get_logger
from rcm.work import exceptions
from rcm.work.models import WorkItemKind

log = get_logger(__name__)

#: Sent and not yet adjudicated: the window in which a correction can still
#: replace the claim before the payer decides it.
REVIEWED_STATES: Final[frozenset[ClaimState]] = frozenset(
    {ClaimState.SUBMITTED, ClaimState.IN_ADJUDICATION}
)

#: Where a rule may send its finding (rules.definition.ROUTES). Anything else
#: lands in the scrubber queue rather than nowhere.
_ROUTED: Final[dict[str, WorkItemKind]] = {
    "SCRUB_EXCEPTION": WorkItemKind.SCRUB_EXCEPTION,
    "AUTH_PROBLEM": WorkItemKind.AUTH_PROBLEM,
    "ELIGIBILITY_PROBLEM": WorkItemKind.ELIGIBILITY_PROBLEM,
}

DETAIL: Final = (
    "Found by the scrubber after the claim was sent. Correct and replace it before the "
    "payer decides, or wait for the 835 if the finding does not apply."
)


@dataclass(slots=True)
class ScrubReviewReport:
    claims_reviewed: int = 0
    claims_blocked: int = 0
    exceptions_raised: int = 0


def _where(finding: Finding) -> str:
    return "the claim" if finding.line_number is None else f"line {finding.line_number}"


def problem_text(finding: Finding) -> str:
    """BR-EXC-01: the rule, its version, where, and what it says."""
    reference = f" ({finding.reference})" if finding.reference else ""
    return (
        f"Scrubber rule {finding.rule_id} v{finding.rule_version}{reference} blocks "
        f"{_where(finding)}: {finding.message}"
    )


def _reviewed(
    session: Session, *, claim_ids: list[uuid.UUID], logic_version: str
) -> set[uuid.UUID]:
    """The claims already reviewed under this ruleset, in one query. Review
    scrubs only: a correct-mode scrub (ADR-0018) raises nothing, so it must
    not stand in for a review."""
    if not claim_ids:
        return set()
    return set(
        session.scalars(
            select(DecisionRecord.entity_id).where(
                DecisionRecord.entity_type == "claim",
                DecisionRecord.entity_id.in_(claim_ids),
                DecisionRecord.decision_type == str(DecisionType.SCRUB),
                DecisionRecord.logic_version == logic_version,
                DecisionRecord.input_snapshot["mode"].astext == "review",
            )
        ).all()
    )


def review_claim(
    session: Session, *, claim: Claim, now: datetime, ruleset: LoadedRuleset | None = None
) -> list[Finding]:
    """Scrub one sent claim without changing it; raise its blocking findings."""
    result = scrub_claim(
        session,
        claim_id=claim.claim_id,
        client_id=claim.client_id,
        today=now.date(),
        apply_corrections=False,
        ruleset=ruleset,
    )
    blocking = [f for f in result.evaluation.findings if f.mode is Mode.BLOCKING]
    for finding in blocking:
        raise_one = partial(
            exceptions.raise_exception,
            session,
            client_id=claim.client_id,
            kind=_ROUTED.get(finding.route_to, WorkItemKind.SCRUB_EXCEPTION),
            record=exceptions.RecordRef("claim", claim.claim_id),
            source_key=f"scrub:{finding.rule_id}:{finding.line_number or 0}:{claim.claim_id}",
            now=now,
            claim_id=claim.claim_id,
            detail=DETAIL,
        )
        try:
            raise_one(problem=problem_text(finding))
        except ValidationFailedError as refused:
            if refused.context.get("field") != "problem":
                raise
            # PD-13 refused the rule's own wording (a date in a reference, say).
            # The item is still raised, named by rule and line; the full
            # message is in the scrub's decision_record.
            log.warning("scrub_review.wording_refused", rule_id=finding.rule_id)
            raise_one(
                problem=f"Scrubber rule {finding.rule_id} v{finding.rule_version} blocks "
                f"{_where(finding)}"
            )
    return blocking


def review_client(session: Session, *, client_id: uuid.UUID, now: datetime) -> ScrubReviewReport:
    """Review every sent, unadjudicated claim of one client not yet reviewed
    under the latest ruleset. Idempotent: a second run finds nothing new."""
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    # No ruleset is an error, never a clean bill of health (PD-18).
    ruleset = load_ruleset(session)
    logic_version = f"ruleset-v{ruleset.version}"
    claims: Sequence[Claim] = session.scalars(
        select(Claim)
        .where(
            Claim.client_id == client_id,
            Claim.state.in_([str(s) for s in REVIEWED_STATES]),
        )
        .order_by(Claim.created_at, Claim.claim_id)
    ).all()

    done = _reviewed(session, claim_ids=[c.claim_id for c in claims], logic_version=logic_version)
    report = ScrubReviewReport()
    for claim in claims:
        if claim.claim_id in done:
            continue
        blocking = review_claim(session, claim=claim, now=now, ruleset=ruleset)
        report.claims_reviewed += 1
        if blocking:
            report.claims_blocked += 1
            report.exceptions_raised += len(blocking)

    log.info(
        "scrub_review.done",
        claims_reviewed=report.claims_reviewed,
        claims_blocked=report.claims_blocked,
        exceptions=report.exceptions_raised,
    )
    return report
