"""Was the patient covered on the date of service? A Layer 5 check
(DOC-02 M3, SRS 8.2; ADR-0034).

The scrub reads the latest 271 the payer sent about the claim's member, as
of the day of the scrub:

| The 271 says | Finding |
|---|---|
| no coverage on the date of service | BLOCKING: verify coverage before sending (EC-13) |
| only that it could not look (AAA) | ADVISORY: no answer is not "not covered" (PD-18) |
| covered, with no period or service detail | ADVISORY: confirmed only generically (EC-15) |
| covered on the date of service, with detail | nothing |

**No 271 on file is no finding.** Eligibility is checked where a payer has
answered; a claim never enquired about is not blocked for it. Requiring a
271 before every claim is a business rule the specifications leave to the
practice, and the synthetic corpus carries none.

The facts a rule reads hold no PHI (ADR-0007), so the member is not among
them: the check is built for one claim, from its member and payer, and its
findings and snapshot name the response by id and dates only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.decision.rules.definition import Layer
from rcm.decision.rules.evaluate import ClaimFacts, Finding, Mode, Outcome
from rcm.domain.claim import Claim
from rcm.domain.eligibility import EligibilityResponseRecord
from rcm.ingestion.x12.eligibility import ACTIVE_COVERAGE_CODES, INACTIVE_COVERAGE_CODES

ELIGIBILITY_CHECK: Final = "ELIGIBILITY_ON_DOS"
_ROUTE: Final = "ELIGIBILITY_PROBLEM"

#: EB03 service type 30, "health benefit plan coverage": the answer to a
#: generic enquiry, saying nothing about a particular service.
GENERIC_SERVICE_TYPES: Final = frozenset({"", "30"})

_NOT_COVERED: Final = (
    "The payer's eligibility response shows no coverage on the date of service; "
    "verify coverage before sending"
)
_NO_ANSWER: Final = (
    "The payer could not answer the eligibility enquiry; that is not a negative answer, "
    "so verify coverage before sending"
)
_GENERIC: Final = (
    "The payer confirmed coverage only generically, with no period or service detail; "
    "confirm the service is covered"
)
_NO_DATE: Final = "The claim has no date of service, so coverage on it cannot be read"


def _date(text: object) -> date | None:
    return date.fromisoformat(str(text)) if text else None


def _covers(periods: list[dict[str, Any]], on: date) -> bool:
    """Every period of the benefit admits the date. No period excludes it."""
    for period in periods:
        start, end = _date(period.get("start")), _date(period.get("end"))
        if (start is not None and on < start) or (end is not None and on > end):
            return False
    return True


def _code(benefit: dict[str, Any]) -> str:
    return str(benefit.get("code", "")).strip().upper()


@dataclass(frozen=True, slots=True)
class Coverage:
    """The latest 271 on file for one claim's member, as the check reads it."""

    response_id: uuid.UUID
    received_on: date
    answered: bool
    benefits: tuple[dict[str, Any], ...]

    def active_on(self, on: date) -> bool:
        return any(
            _code(b) in ACTIVE_COVERAGE_CODES and _covers(list(b.get("periods") or []), on)
            for b in self.benefits
        )

    def inactive_on(self, on: date) -> bool:
        """An inactive benefit admits the date, or no active one does."""
        explicit = any(
            _code(b) in INACTIVE_COVERAGE_CODES and _covers(list(b.get("periods") or []), on)
            for b in self.benefits
        )
        return explicit or not self.active_on(on)

    @property
    def generic(self) -> bool:
        active = [b for b in self.benefits if _code(b) in ACTIVE_COVERAGE_CODES]
        return bool(active) and all(
            not b.get("periods") and str(b.get("service_type", "")) in GENERIC_SERVICE_TYPES
            for b in active
        )


@dataclass(frozen=True, slots=True)
class EligibilityCheck:
    """Coverage on the date of service, from the latest 271 (Layer 5)."""

    coverage: Coverage | None
    layer: Layer = field(default=Layer.TEMPORAL)

    def _finding(self, coverage: Coverage, mode: Mode, outcome: Outcome, message: str) -> Finding:
        return Finding(
            rule_id=ELIGIBILITY_CHECK,
            rule_version=0,
            layer=Layer.TEMPORAL,
            mode=mode,
            outcome=outcome,
            line_number=None,
            message=message,
            route_to=_ROUTE,
            reference=f"eligibility-271 {coverage.response_id}",
        )

    def findings(self, facts: ClaimFacts) -> list[Finding]:
        coverage = self.coverage
        if coverage is None:
            return []
        if not coverage.answered:
            return [self._finding(coverage, Mode.ADVISORY, Outcome.FLAGGED, _NO_ANSWER)]
        on = facts.claim["claim.date_of_service"]
        if not isinstance(on, date):
            # Not a pass (ADR-0007 Decision 5): the question cannot be read.
            return [self._finding(coverage, Mode.ADVISORY, Outcome.INDETERMINATE, _NO_DATE)]
        if coverage.inactive_on(on):
            return [self._finding(coverage, Mode.BLOCKING, Outcome.FLAGGED, _NOT_COVERED)]
        if coverage.generic:
            return [self._finding(coverage, Mode.ADVISORY, Outcome.FLAGGED, _GENERIC)]
        return []

    def snapshot(self, facts: ClaimFacts) -> dict[str, object] | None:
        """What the claim's decision record keeps: the response, not the member."""
        coverage = self.coverage
        if coverage is None:
            return None
        on = facts.claim["claim.date_of_service"]
        return {
            "response_id": str(coverage.response_id),
            "received_on": coverage.received_on.isoformat(),
            "answered": coverage.answered,
            "active_on_date_of_service": (
                coverage.active_on(on) if isinstance(on, date) and coverage.answered else None
            ),
        }


def latest_coverage(
    session: Session, *, client_id: uuid.UUID, payer_id: str, member_id: str, as_of: date
) -> Coverage | None:
    """The latest 271 about this member from this payer, received on or
    before `as_of`. Scoped to the client (PD-20)."""
    before = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=UTC)
    row = session.scalar(
        select(EligibilityResponseRecord)
        .where(
            EligibilityResponseRecord.client_id == client_id,
            EligibilityResponseRecord.payer_id == payer_id,
            EligibilityResponseRecord.member_id == member_id,
            EligibilityResponseRecord.received_at < before,
        )
        .order_by(
            EligibilityResponseRecord.received_at.desc(),
            EligibilityResponseRecord.created_at.desc(),
        )
        .limit(1)
    )
    if row is None:
        return None
    return Coverage(
        response_id=row.response_id,
        received_on=row.received_at.astimezone(UTC).date(),
        answered=row.answered,
        benefits=tuple(row.benefits),
    )


def check_for(
    session: Session, *, claim_id: uuid.UUID, client_id: uuid.UUID, today: date
) -> EligibilityCheck:
    """The eligibility check for one claim, as of `today`."""
    claim = session.scalar(
        select(Claim).where(Claim.claim_id == claim_id, Claim.client_id == client_id)
    )
    if claim is None:
        return EligibilityCheck(coverage=None)
    return EligibilityCheck(
        coverage=latest_coverage(
            session,
            client_id=client_id,
            payer_id=claim.payer_id,
            member_id=claim.subscriber_member_id,
            as_of=today,
        )
    )
