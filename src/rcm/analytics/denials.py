"""Denial rate, counted per claim rather than per submission (M1.9).

This module exists because of one sentence in DOC-02:

    BR-DATA-03 (MUST): Resubmissions MUST be chained to the original claim,
    not stored as independent claims. Failure to do this inflates volume and
    distorts the denial rate.

The distortion is worth stating as a number rather than a warning. On the
synthetic corpus:

    submissions            616        denied submissions   107    17.37%
    claims                 600        denied claims         91    15.17%

Resubmitting a denied claim does not deny it twice. The per-submission figure
counts the same denial again every time the claim is sent back, so it is
always higher than the truth, always internally consistent, and always wrong in
the direction that makes the operation look worse than it is. Nothing
downstream can detect it: both numbers are real counts of real rows.

`DenialRate` therefore reports both, and names the wrong one `per_submission`
rather than hiding it. A reconciliation tool comparing this platform against a
previous vendor's reporting needs the wrong number to explain the difference,
and someone who has it in front of them labelled is much less likely to
recompute it by accident than someone who does not know it exists.

**What counts as a denial.** DOC-02's glossary: *"Payer refusal to pay a line
or claim, with a reason code."* So a service line is denied when the payer paid
nothing on it and gave a refusal reason -- both halves matter. A line paid at
zero with no reason code is not an adjudicated denial, a line carrying a reason
code that was still paid is an adjustment, and a reason that is the patient's
cost share or a payment adjustment is not a refusal (ADR-0012). A denial on a
posting the payer later reversed still happened, and still counts. On the
corpus this rule reproduces the generator's own `is_denied` flag exactly: 135
lines, 107 submissions, no disagreement in either direction.

That is a structural signal, not a taxonomy. Classifying *why* a claim was
denied, and whether it was preventable, is M4.8's job. This module only answers
"how often", and does it on the right denominator.

**PD-4 is not this module's to break.** Rejections never reach `claim_payment`
-- a rejection happens before adjudication, arrives on a 277CA, and is a
distinct outcome type. Counting them here would conflate the two and hide
front-end data quality problems behind a payer-behaviour metric.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import ColumnElement, Select, and_, distinct, exists, func, select
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim
from rcm.domain.remittance import ClaimPayment, RemittanceAdjustment, RemittanceServiceLine

#: ADR-0012. The patient's cost share: the payer approved the service and the
#: patient owes deductible (1), coinsurance (2) or copay (3). A line paid
#: nothing for these reasons alone was not refused. Any other reason --
#: including PR-204, "not covered under the plan" -- is a refusal; group PR
#: says who may be billed, not whether the payer refused.
#:
#: The one definition. Both dbt models spell it the same way, and
#: tests/unit/test_denial_taxonomy.py fails if they drift.
COST_SHARE: Final = frozenset({("PR", "1"), ("PR", "2"), ("PR", "3")})

#: ADR-0012. Payment adjustments: the payer priced the service, it did not
#: refuse it. 45 is the fee-schedule cut that rides on nearly every paid line
#: -- and on a line the deductible swallowed whole, beside PR-1. 23 is a prior
#: payer having paid already (a secondary paying nothing is the correct
#: outcome), 24 is capitation. Read by CARC whatever the group, as the payer's
#: group on these varies. Spelled the same in both dbt models.
PAYMENT_ADJUSTMENT: Final = frozenset({"23", "24", "45"})


def is_refusal(group: str, carc: str) -> bool:
    """Whether one adjustment reason is a refusal to pay (ADR-0012)."""
    return (group, carc) not in COST_SHARE and carc not in PAYMENT_ADJUSTMENT


_HUNDRED = Decimal("100")
_PERCENT_PLACES = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class DenialRate:
    """Denied and adjudicated counts, at both grains.

    `claims` and `denied_claims` are the real ones. The `*_submissions` pair is
    kept so the difference can be explained rather than merely avoided.
    """

    claims: int
    denied_claims: int
    submissions: int
    denied_submissions: int

    @property
    def rate(self) -> Decimal:
        """The denial rate: denied claims over adjudicated claims.

        Zero adjudicated claims is reported as zero rather than raising. An
        empty period is a real answer to "what was the denial rate", and a
        dashboard that throws on a quiet week is a dashboard people route
        around.
        """
        if self.claims == 0:
            return Decimal("0.00")
        return self._percentage(self.denied_claims, self.claims)

    @property
    def per_submission(self) -> Decimal:
        """What the denial rate looks like if resubmissions are counted twice.

        **Not the denial rate.** Exposed so the gap against `rate` can be
        quoted when reconciling against a system that does not chain
        resubmissions -- which is most of them.
        """
        if self.submissions == 0:
            return Decimal("0.00")
        return self._percentage(self.denied_submissions, self.submissions)

    @property
    def inflation(self) -> Decimal:
        """Percentage points by which the per-submission figure overstates."""
        return self.per_submission - self.rate

    @staticmethod
    def _percentage(numerator: int, denominator: int) -> Decimal:
        return ((Decimal(numerator) / Decimal(denominator)) * _HUNDRED).quantize(_PERCENT_PLACES)


def _denied_submitter_claim_ids(client_id: uuid.UUID) -> Select[str]:
    """CLM01s whose remittance shows a refusal to pay with a reason.

    Returned as a subquery over `claim_payment.claim_id` because that is the
    payer's echo of the claim number the submitter sent -- the only link
    between the 835 side and the 837 side until M1.10 matches on the payer's
    own control number as well.
    """
    return (
        # `select(...).distinct()` rather than `select(distinct(...))`: the
        # unary form is a column expression, warns outside an aggregate, and
        # the suite turns warnings into errors.
        select(ClaimPayment.claim_id)
        .distinct()
        .join(
            RemittanceServiceLine,
            RemittanceServiceLine.claim_payment_id == ClaimPayment.claim_payment_id,
        )
        .where(
            ClaimPayment.client_id == client_id,
            RemittanceServiceLine.client_id == client_id,
            # A reversal is the payer taking a payment back, not refusing to
            # make one, and its amounts are the original's with the signs
            # flipped. A line that was paid nothing originally is still paid
            # nothing when negated, and its adjustments come back negated too,
            # so without this a reversal reads as fresh evidence of a denial
            # and the claim is counted again (BR-M9-04).
            #
            # On the current corpus exactly one reversal line meets that
            # description, and it belongs to a claim already counted -- so the
            # rate does not move today. That is coincidence, not safety.
            ClaimPayment.is_reversal.is_(False),
            # Paid nothing...
            RemittanceServiceLine.paid_amount == 0,
            # ...and said why. Both halves: a zero payment with no reason is
            # not an adjudicated denial, and a reason code on a line that was
            # still paid is an adjustment rather than a refusal.
            exists().where(
                and_(
                    RemittanceAdjustment.service_line_id == RemittanceServiceLine.service_line_id,
                    RemittanceAdjustment.client_id == client_id,
                    # ...a refusal, not the patient's cost share (ADR-0012).
                    refusal_clause(),
                )
            ),
        )
    )


def refusal_clause() -> ColumnElement[bool]:
    """SQL for `is_refusal` on a `RemittanceAdjustment` row."""
    return and_(
        ~and_(
            RemittanceAdjustment.cas_group_code == "PR",
            RemittanceAdjustment.reason_code.in_(sorted(carc for _, carc in COST_SHARE)),
        ),
        RemittanceAdjustment.reason_code.not_in(sorted(PAYMENT_ADJUSTMENT)),
    )


def denied_submitter_claim_ids(client_id: uuid.UUID) -> Select[str]:
    """The same definition of a denial, for the rules backtest (M4.4).

    One definition, so a backtest's true positives and the denial rate count
    the same thing.
    """
    return _denied_submitter_claim_ids(client_id)


def denial_rate(session: Session, *, client_id: uuid.UUID) -> DenialRate:
    """Denial rate for one client, counted per claim (BR-DATA-03).

    Scoped by `client_id` explicitly rather than through `scoped_query`,
    because this is an aggregate over two tables rather than a select of one
    entity; every `WHERE` here carries the client, and the test in
    `tests/security` asserts that a second client's rows cannot reach the
    result.
    """
    denied_ids = _denied_submitter_claim_ids(client_id)

    # Adjudicated claims: those with a remittance. A claim still in flight has
    # not been denied *or* paid, and including it in the denominator reports a
    # falling denial rate every time volume grows.
    adjudicated = (
        select(Claim)
        .where(
            Claim.client_id == client_id,
            exists().where(
                and_(
                    ClaimPayment.claim_id == Claim.submitter_claim_id,
                    ClaimPayment.client_id == client_id,
                )
            ),
        )
        .subquery()
    )

    totals = session.execute(
        select(
            func.count(distinct(adjudicated.c.root_claim_id)),
            func.count(),
            func.count(distinct(adjudicated.c.root_claim_id)).filter(
                adjudicated.c.submitter_claim_id.in_(denied_ids)
            ),
            func.count().filter(adjudicated.c.submitter_claim_id.in_(denied_ids)),
        ).select_from(adjudicated)
    ).one()

    return DenialRate(
        claims=totals[0],
        submissions=totals[1],
        denied_claims=totals[2],
        denied_submissions=totals[3],
    )
