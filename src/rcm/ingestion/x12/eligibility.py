"""271 eligibility and benefit response: is this patient covered (M1.6).

A 271 answers the question asked before the claim exists, and getting it wrong
is expensive in a way that shows up months later. DOC-02's eligibility module
lists the failure directly: coverage terminated before the date of service,
member identifiers that do not match, and a payer that answers "I cannot find
this person" rather than yes or no.

Three readings this module keeps apart, because collapsing them is how an
eligibility check becomes a rubber stamp.

**Active is not the absence of inactive.** `EB01` is a coverage code, not a
boolean. `1` is active coverage; `6`, `7` and `8` are three different kinds of
inactive, and `I` is non-covered. A parser that reads "not 6" as active
reports coverage for a patient whose plan is pending investigation.

**A rejected enquiry is not a negative answer.** `AAA` segments mean the payer
could not process the question -- the member id was invalid, the name did not
match, the payer is unavailable. "We could not look" and "they are not
covered" lead to opposite actions, and the second one bills a patient who is
insured.

**A benefit without its date range says nothing useful.** Coverage is a
period, and a 271 that answers about today tells you nothing about a date of
service three weeks ago. `DTP` is read and kept with each benefit so that the
question "was this patient covered on the date of service" stays answerable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

from rcm.ingestion.x12.envelope import TransactionSet
from rcm.ingestion.x12.fields import optional_amount, optional_date
from rcm.ingestion.x12.tokenizer import Separators

#: EB01 eligibility or benefit information codes (X12 code list 1390) that
#: mean the patient is covered.
ACTIVE_COVERAGE_CODES: Final[frozenset[str]] = frozenset(
    {
        "1",  # active coverage
        "2",  # active, full risk capitation
        "3",  # active, services capitated
        "4",  # active, services capitated to primary care physician
        "5",  # active, pending investigation
    }
)

#: Codes that mean the patient is not covered. `5` is deliberately *not* here
#: and `7` deliberately is: "active, pending investigation" still pays, while
#: "inactive, pending eligibility update" does not. The two read almost
#: identically and mean opposite things.
INACTIVE_COVERAGE_CODES: Final[frozenset[str]] = frozenset(
    {
        "6",  # inactive
        "7",  # inactive, pending eligibility update
        "8",  # inactive, pending investigation
        "I",  # non-covered
        "V",  # cannot process
    }
)

#: DTP01 qualifiers that carry a coverage period.
_PLAN_PERIOD_QUALIFIERS: Final[frozenset[str]] = frozenset({"291", "292", "346", "347", "435"})


@dataclass(frozen=True, slots=True)
class BenefitPeriod:
    """A DTP attached to a benefit: when this coverage applied."""

    qualifier: str
    start: date | None = None
    end: date | None = None

    def covers(self, when: date) -> bool:
        """Was this benefit in force on `when`?

        An open end date means still in force, which is the common case; an
        open start means the payer did not say, and the only honest reading
        of that is that the period does not exclude the date.
        """
        if self.start is not None and when < self.start:
            return False
        return not (self.end is not None and when > self.end)


@dataclass(frozen=True, slots=True)
class Benefit:
    """One EB segment: a statement about what is covered."""

    #: EB01, the eligibility or benefit code. The element that decides.
    code: str
    #: EB02 coverage level, EB03 service type, EB04 insurance type.
    coverage_level: str = ""
    service_type: str = ""
    plan_coverage: str = ""
    #: EB05, the plan description the payer gives.
    description: str = ""
    #: EB06 time period qualifier and EB07 the monetary amount, which carry
    #: deductibles, copays and out-of-pocket figures.
    time_period: str = ""
    amount: Decimal | None = None
    #: EB08 percent, as a ratio.
    percent: Decimal | None = None
    periods: tuple[BenefitPeriod, ...] = ()

    @property
    def is_active(self) -> bool:
        return self.code.strip().upper() in ACTIVE_COVERAGE_CODES

    @property
    def is_inactive(self) -> bool:
        return self.code.strip().upper() in INACTIVE_COVERAGE_CODES

    @property
    def states_coverage(self) -> bool:
        """Does this benefit say anything about whether the patient is covered?

        Most EB segments do not: they describe a copay, a deductible or a
        limitation. Only the coverage codes answer the eligibility question,
        and treating the rest as silence is what keeps a copay amount from
        being read as proof of coverage.
        """
        return self.is_active or self.is_inactive

    def covered_on(self, when: date) -> bool:
        """Active, and in force on the given date.

        The date matters: a 271 answering about today says nothing about a
        date of service three weeks ago, and a benefit with no period at all
        is treated as not excluding the date rather than as proof.
        """
        if not self.is_active:
            return False
        return all(period.covers(when) for period in self.periods)


@dataclass(frozen=True, slots=True)
class RequestProblem:
    """An AAA segment: the payer could not answer the question asked.

    Not a negative answer. "We could not look" and "they are not covered"
    lead to opposite actions, and the second one bills an insured patient.
    """

    #: AAA01, whether the request may be resubmitted as-is.
    valid_request: bool
    #: AAA03, why the payer could not process it.
    reject_reason_code: str = ""
    #: AAA04, what the sender should do next.
    follow_up_action: str = ""


@dataclass(frozen=True, slots=True)
class EligibilityResponse:
    """One 271 transaction set."""

    transaction_control_number: str
    reference: str = ""
    created_on: date | None = None
    payer_name: str = ""
    payer_identifier: str = ""
    subscriber_last_name: str = ""
    subscriber_first_name: str = ""
    member_id: str = ""
    date_of_birth: date | None = None
    benefits: tuple[Benefit, ...] = ()
    problems: tuple[RequestProblem, ...] = ()

    @property
    def answered(self) -> bool:
        """Did the payer actually answer the question?

        A response carrying only `AAA` problems is a refusal to look, and
        reading it as "not covered" is the mistake this property exists to
        make hard.
        """
        return any(benefit.states_coverage for benefit in self.benefits)

    @property
    def is_active(self) -> bool:
        """Does the response state active coverage?

        False when the payer could not answer, which is **not** the same as
        stating the patient is uncovered -- use `answered` to tell those
        apart before acting on this (PD-18).
        """
        return any(benefit.is_active for benefit in self.benefits)

    def active_on(self, when: date) -> bool:
        """Was coverage in force on a given date, usually the date of service."""
        return any(benefit.covered_on(when) for benefit in self.benefits)


@dataclass
class _BenefitBuilder:
    """Mutable accumulator for one EB loop and the DTPs that follow it."""

    code: str
    coverage_level: str = ""
    service_type: str = ""
    plan_coverage: str = ""
    description: str = ""
    time_period: str = ""
    amount: Decimal | None = None
    percent: Decimal | None = None
    periods: list[BenefitPeriod] = field(default_factory=list)

    def build(self) -> Benefit:
        return Benefit(
            code=self.code,
            coverage_level=self.coverage_level,
            service_type=self.service_type,
            plan_coverage=self.plan_coverage,
            description=self.description,
            time_period=self.time_period,
            amount=self.amount,
            percent=self.percent,
            periods=tuple(self.periods),
        )


def _parse_period(raw: str, qualifier: str, format_qualifier: str) -> BenefitPeriod:
    """DTP03 under a D8 or RD8 format qualifier.

    `RD8` is a range written `CCYYMMDD-CCYYMMDD`; `D8` is a single date, which
    for a coverage period means a start with no stated end.
    """
    text = raw.strip()
    if format_qualifier.strip().upper() == "RD8" and "-" in text:
        start_text, _, end_text = text.partition("-")
        return BenefitPeriod(
            qualifier=qualifier,
            start=_date_or_none(start_text),
            end=_date_or_none(end_text),
        )
    return BenefitPeriod(qualifier=qualifier, start=_date_or_none(text), end=None)


def _date_or_none(text: str) -> date | None:
    """A CCYYMMDD date, or `None` where the sender left it open.

    Deliberately lenient: a malformed date inside a coverage period makes the
    period unusable, not the file unreadable, and refusing the whole 271 over
    it would lose the benefits that parsed.
    """
    cleaned = text.strip()
    expected = 8
    if len(cleaned) != expected or not cleaned.isdigit():
        return None
    try:
        return date(int(cleaned[0:4]), int(cleaned[4:6]), int(cleaned[6:8]))
    except ValueError:
        return None


def parse_271(transaction: TransactionSet, separators: Separators) -> EligibilityResponse:
    """Read one 271 eligibility and benefit response."""
    del separators  # the elements this parser reads are not composites

    reference = ""
    created_on: date | None = None
    payer_name = payer_identifier = ""
    last_name = first_name = member_id = ""
    date_of_birth: date | None = None

    benefits: list[_BenefitBuilder] = []
    problems: list[RequestProblem] = []
    current: _BenefitBuilder | None = None

    for segment in transaction.segments:
        tag = segment.tag

        if tag == "BHT":
            reference = segment.get(3).strip()
            created_on = optional_date(segment.get(4), field_name="BHT04", segment=segment)

        elif tag == "NM1":
            entity = segment.get(1).strip()
            if entity == "PR":
                payer_name = segment.get(3).strip()
                payer_identifier = segment.get(9).strip()
            elif entity == "IL":
                last_name = segment.get(3).strip()
                first_name = segment.get(4).strip()
                member_id = segment.get(9).strip()

        elif tag == "DMG":
            date_of_birth = optional_date(segment.get(2), field_name="DMG02", segment=segment)

        elif tag == "EB":
            current = _BenefitBuilder(
                code=segment.get(1).strip(),
                coverage_level=segment.get(2).strip(),
                service_type=segment.get(3).strip(),
                plan_coverage=segment.get(4).strip(),
                description=segment.get(5).strip(),
                time_period=segment.get(6).strip(),
                amount=(
                    optional_amount(segment.get(7), field_name="EB07", segment=segment)
                    if segment.get(7).strip()
                    else None
                ),
                percent=(
                    optional_amount(segment.get(8), field_name="EB08", segment=segment)
                    if segment.get(8).strip()
                    else None
                ),
            )
            benefits.append(current)

        elif tag == "DTP":
            qualifier = segment.get(1).strip()
            if current is not None and qualifier in _PLAN_PERIOD_QUALIFIERS:
                current.periods.append(_parse_period(segment.get(3), qualifier, segment.get(2)))

        elif tag == "AAA":
            problems.append(
                RequestProblem(
                    valid_request=segment.get(1).strip().upper() == "Y",
                    reject_reason_code=segment.get(3).strip(),
                    follow_up_action=segment.get(4).strip(),
                )
            )

    return EligibilityResponse(
        transaction_control_number=transaction.control_number,
        reference=reference,
        created_on=created_on,
        payer_name=payer_name,
        payer_identifier=payer_identifier,
        subscriber_last_name=last_name,
        subscriber_first_name=first_name,
        member_id=member_id,
        date_of_birth=date_of_birth,
        benefits=tuple(builder.build() for builder in benefits),
        problems=tuple(problems),
    )
