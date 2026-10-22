"""837P professional claim: parser and generator (M1.5).

The acceptance condition for this module is a round trip -- *"generate → parse
→ equals source claim"* (CLAUDE.md M1.5) -- which is why both directions live
in one file. A round trip across two modules proves they agree with each other;
a round trip within one module proves the module's understanding of the format
is self-consistent, and that is only worth something because the parser is
*also* checked against 325 files written by the synthetic generator, which
shares no code with it. Agreement with itself and agreement with an independent
writer are different claims, and M1 needs both.

Three decisions this module exists to get right.

**The resubmission reference is never dropped (PD-7).** `REF*F8` in loop 2300
carries the payer's claim number for the claim this one replaces. A platform
that loses it counts three submissions of one claim as three claims, which
deflates the denial rate by exactly the amount that matters -- the claims that
had to be sent again are the ones that were denied. `original_claim_id` is a
first-class field, and `is_resubmission` reads off it.

**Modifiers are transcribed, never supplied (PD-16).** The generator writes the
modifiers it is given and no others. There is no code path here that appends a
25 or a 59, because the point of PD-16 is that attaching those is a coding
judgement a human makes and signs for.

**Diagnosis pointers are reported, not judged.** BR-M6-01 requires every
service line to carry at least one. This module exposes
`lines_without_diagnosis_pointer` rather than raising, for the same reason the
835 parser exposes `balances` rather than quarantining: reporting what a file
says and deciding what to do about it are different jobs, and keeping them
apart is what lets a reconciliation tool read a broken file deliberately.

What this module deliberately refuses: a dependent loop (`HL*23`), where the
patient is someone other than the subscriber. The demographics would have to
come from loop 2010CA, which is not read here yet, and borrowing the
subscriber's would produce a well-formed claim about the wrong patient -- an
error no later stage could detect. It raises `UnsupportedX12Error`, which says
the gap is ours rather than the sender's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

from rcm.ingestion.x12.envelope import TransactionSet
from rcm.ingestion.x12.fields import (
    optional_date,
    parse_amount,
    parse_date,
    parse_quantity,
)
from rcm.ingestion.x12.tokenizer import Segment, Separators
from rcm.platform.errors import MalformedX12Error, UnsupportedX12Error

#: HL03 hierarchical level codes used by 837P.
_HL_BILLING_PROVIDER: Final = "20"
_HL_SUBSCRIBER: Final = "22"
_HL_DEPENDENT: Final = "23"

#: HI qualifiers for a diagnosis. AB* are ICD-10, the bare forms are ICD-9,
#: which still appears on old corrected claims.
_PRINCIPAL_DIAGNOSIS: Final = frozenset({"ABK", "BK"})
_SECONDARY_DIAGNOSIS: Final = frozenset({"ABF", "BF"})

#: SV101 composite: qualifier, procedure code, then up to four modifiers.
_SV1_MAX_MODIFIERS: Final = 4
#: X12 allows up to twelve diagnoses on an 837P claim.
_MAX_DIAGNOSES: Final = 12

_TWO_PLACES: Final = Decimal("0.01")

#: What a generated file declares in its ISA. A parser reads the delimiters out
#: of each file it is given, so these apply to generation only.
STANDARD_SEPARATORS: Final = Separators(element="*", component=":", repetition="^", segment="~")


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradingPartner:
    """Loop 1000A submitter or 1000B receiver."""

    name: str
    #: NM109, the electronic transmitter identification number.
    etin: str
    contact_name: str = ""
    contact_phone: str = ""


@dataclass(frozen=True, slots=True)
class BillingProvider:
    """Loop 2000A/2010AA: who is billing, and under what tax identity."""

    name: str
    npi: str
    taxonomy_code: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    #: REF*EI, the employer identification number the claim is billed under.
    tax_id: str = ""


@dataclass(frozen=True, slots=True)
class Subscriber:
    """Loop 2010BA. The patient too, while this module refuses dependents."""

    last_name: str
    first_name: str
    #: NM109 under qualifier MI: the member identifier the payer knows.
    member_id: str
    address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    date_of_birth: date | None = None
    sex: str = ""


@dataclass(frozen=True, slots=True)
class ClaimServiceLine:
    """Loop 2400: one billed service."""

    line_number: int
    procedure_code: str
    charge_amount: Decimal
    #: SV101 components 3 onward. Transcribed in the order the file carries
    #: them, because modifier order is itself meaningful (DOC-02: pricing
    #: modifiers precede informational ones).
    modifiers: tuple[str, ...] = ()
    #: SV103, the unit basis. "UN" for units, "MJ" for minutes.
    unit_basis: str = "UN"
    units: Decimal = Decimal("1")
    #: SV107, pointing into the claim's diagnosis list, one-based.
    diagnosis_pointers: tuple[int, ...] = ()
    #: DTP*472 where the line carries its own date of service.
    service_date: date | None = None


@dataclass(frozen=True, slots=True)
class Claim837:
    """Loop 2300: one professional claim."""

    claim_id: str
    total_charge: Decimal
    billing_provider: BillingProvider
    subscriber: Subscriber
    payer_id: str
    payer_name: str = ""
    #: SBR09, the claim filing indicator: CI commercial, MC medicaid, MB
    #: medicare part B, and so on.
    claim_filing_code: str = ""
    #: SBR01: P primary, S secondary, T tertiary.
    payer_responsibility: str = "P"
    #: CLM05-1, the place of service code.
    place_of_service: str = ""
    #: CLM05-3, the claim frequency code. 1 original, 7 replacement, 8 void.
    claim_frequency_code: str = "1"
    diagnoses: tuple[str, ...] = ()
    service_date: date | None = None
    prior_auth_number: str = ""
    referral_number: str = ""
    #: REF*F8. PD-7: a resubmission chains to the claim it replaces rather than
    #: being stored as a new claim.
    original_claim_id: str = ""
    lines: tuple[ClaimServiceLine, ...] = ()

    @property
    def is_resubmission(self) -> bool:
        """PD-7: does this claim replace an earlier one?

        Read off the reference rather than the frequency code, because the two
        disagree in practice -- senders set frequency 7 without a reference and
        send a reference with frequency 1. The reference is the one that names
        a specific prior claim, so it is the one that can chain.
        """
        return bool(self.original_claim_id)

    @property
    def line_charge_total(self) -> Decimal:
        """What the service lines add up to, for comparison against CLM02."""
        return sum((line.charge_amount for line in self.lines), Decimal("0.00"))

    @property
    def charges_reconcile(self) -> bool:
        """Does CLM02 equal the sum of the line charges?

        Reported rather than enforced, for the same reason the 835 parser
        reports `balances`: this module says what the file contains and a
        validator decides what that means.
        """
        return self.total_charge == self.line_charge_total

    @property
    def lines_without_diagnosis_pointer(self) -> tuple[int, ...]:
        """Line numbers violating BR-M6-01, if any."""
        return tuple(line.line_number for line in self.lines if not line.diagnosis_pointers)


@dataclass(frozen=True, slots=True)
class ClaimSubmission:
    """One ST...SE 837 transaction: the claims and who sent them."""

    control_number: str
    submitter: TradingPartner
    receiver: TradingPartner
    claims: tuple[Claim837, ...]
    #: Kept as a date plus an HHMM string rather than a datetime, because that
    #: is exactly what the file carries. A datetime would round-trip through
    #: minute precision and lose anything finer without saying so.
    created_on: date | None = None
    created_time: str = "0000"
    #: ISA15: T for test, P for production.
    usage_indicator: str = "T"
    #: ST03, the implementation guide the sender claims to follow.
    implementation_reference: str = "005010X222A1"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class _ClaimBuilder:
    """Mutable accumulator for one 2300 loop."""

    claim_id: str
    total_charge: Decimal
    place_of_service: str
    claim_frequency_code: str
    billing_provider: BillingProvider
    subscriber: Subscriber
    payer_id: str
    payer_name: str
    claim_filing_code: str
    payer_responsibility: str
    diagnoses: list[str] = field(default_factory=list)
    service_date: date | None = None
    prior_auth_number: str = ""
    referral_number: str = ""
    original_claim_id: str = ""
    lines: list[ClaimServiceLine] = field(default_factory=list)

    def build(self) -> Claim837:
        return Claim837(
            claim_id=self.claim_id,
            total_charge=self.total_charge,
            billing_provider=self.billing_provider,
            subscriber=self.subscriber,
            payer_id=self.payer_id,
            payer_name=self.payer_name,
            claim_filing_code=self.claim_filing_code,
            payer_responsibility=self.payer_responsibility,
            place_of_service=self.place_of_service,
            claim_frequency_code=self.claim_frequency_code,
            diagnoses=tuple(self.diagnoses),
            service_date=self.service_date,
            prior_auth_number=self.prior_auth_number,
            referral_number=self.referral_number,
            original_claim_id=self.original_claim_id,
            lines=tuple(self.lines),
        )


@dataclass
class _LineBuilder:
    """Mutable accumulator for one 2400 loop."""

    line_number: int
    procedure_code: str = ""
    charge_amount: Decimal = Decimal("0.00")
    modifiers: tuple[str, ...] = ()
    unit_basis: str = "UN"
    units: Decimal = Decimal("1")
    diagnosis_pointers: tuple[int, ...] = ()
    service_date: date | None = None

    def build(self) -> ClaimServiceLine:
        return ClaimServiceLine(
            line_number=self.line_number,
            procedure_code=self.procedure_code,
            charge_amount=self.charge_amount,
            modifiers=self.modifiers,
            unit_basis=self.unit_basis,
            units=self.units,
            diagnosis_pointers=self.diagnosis_pointers,
            service_date=self.service_date,
        )


@dataclass
class _Section:
    """Which loop the reader is inside.

    N3, N4 and REF all appear in several loops and mean different things in
    each, so they cannot be dispatched on their tag alone. Tracking the loop is
    what keeps a billing provider's address out of a subscriber record.
    """

    billing = "billing"
    subscriber = "subscriber"
    payer = "payer"
    claim = "claim"
    line = "line"
    header = "header"


def _parse_integer_pointers(raw: tuple[str, ...], segment: Segment) -> tuple[int, ...]:
    """SV107 diagnosis pointers, one-based indexes into the claim's HI list."""
    pointers: list[int] = []
    for component in raw:
        text = component.strip()
        if not text:
            continue
        if not text.isdigit():
            raise MalformedX12Error(
                reason="diagnosis pointer is not a number",
                field="SV107",
                segment=str(segment),
            )
        pointers.append(int(text))
    return tuple(pointers)


def parse_837(transaction: TransactionSet, separators: Separators) -> ClaimSubmission:
    """Read one 837 transaction set into claims.

    Raises `MalformedX12Error` for a file that is not valid 837, and
    `UnsupportedX12Error` for one that is valid but uses a structure this
    parser does not read. Every refusal is named: DR-10 requires a quarantined
    file to say which assertion it failed, and an unnamed exception names
    nothing.
    """
    submitter = TradingPartner(name="", etin="")
    receiver = TradingPartner(name="", etin="")
    billing = BillingProvider(name="", npi="")
    subscriber = Subscriber(last_name="", first_name="", member_id="")
    payer_id = ""
    payer_name = ""
    claim_filing_code = ""
    payer_responsibility = "P"
    created_on: date | None = None
    created_time = "0000"

    section = _Section.header
    claims: list[_ClaimBuilder] = []
    claim: _ClaimBuilder | None = None
    line: _LineBuilder | None = None

    def close_line() -> None:
        nonlocal line
        if line is not None and claim is not None:
            claim.lines.append(line.build())
        line = None

    for segment in transaction.segments:
        tag = segment.tag

        if tag == "BHT":
            created_on = optional_date(segment.get(4), field_name="BHT04", segment=segment)
            created_time = segment.get(5).strip() or "0000"

        elif tag == "HL":
            close_line()
            level = segment.get(3).strip()
            if level == _HL_DEPENDENT:
                raise UnsupportedX12Error(
                    reason="dependent loop (HL*23): the patient is not the subscriber",
                    field="HL03",
                    segment=str(segment),
                )
            if level == _HL_BILLING_PROVIDER:
                section = _Section.billing
                billing = BillingProvider(name="", npi="")
            elif level == _HL_SUBSCRIBER:
                section = _Section.subscriber
                subscriber = Subscriber(last_name="", first_name="", member_id="")
            else:
                raise MalformedX12Error(
                    reason=f"unknown hierarchical level code {level!r}",
                    field="HL03",
                    segment=str(segment),
                )

        elif tag == "PRV" and segment.get(1).strip() == "BI":
            billing = _replace_billing(billing, taxonomy_code=segment.get(3).strip())

        elif tag == "NM1":
            entity = segment.get(1).strip()
            if entity == "41":
                submitter = TradingPartner(name=segment.get(3).strip(), etin=segment.get(9).strip())
            elif entity == "40":
                receiver = TradingPartner(name=segment.get(3).strip(), etin=segment.get(9).strip())
            elif entity == "85":
                section = _Section.billing
                billing = _replace_billing(
                    billing, name=segment.get(3).strip(), npi=segment.get(9).strip()
                )
            elif entity == "IL":
                section = _Section.subscriber
                subscriber = _replace_subscriber(
                    subscriber,
                    last_name=segment.get(3).strip(),
                    first_name=segment.get(4).strip(),
                    member_id=segment.get(9).strip(),
                )
            elif entity == "PR":
                section = _Section.payer
                payer_name = segment.get(3).strip()
                payer_id = segment.get(9).strip()

        elif tag == "PER" and section in {_Section.header, _Section.billing}:
            if submitter.etin and not submitter.contact_name:
                submitter = TradingPartner(
                    name=submitter.name,
                    etin=submitter.etin,
                    contact_name=segment.get(2).strip(),
                    contact_phone=segment.get(4).strip(),
                )

        elif tag == "SBR":
            payer_responsibility = segment.get(1).strip() or "P"
            claim_filing_code = segment.get(9).strip()

        elif tag == "N3":
            if section == _Section.billing:
                billing = _replace_billing(billing, address=segment.get(1).strip())
            elif section == _Section.subscriber:
                subscriber = _replace_subscriber(subscriber, address=segment.get(1).strip())

        elif tag == "N4":
            if section == _Section.billing:
                billing = _replace_billing(
                    billing,
                    city=segment.get(1).strip(),
                    state=segment.get(2).strip(),
                    postal_code=segment.get(3).strip(),
                )
            elif section == _Section.subscriber:
                subscriber = _replace_subscriber(
                    subscriber,
                    city=segment.get(1).strip(),
                    state=segment.get(2).strip(),
                    postal_code=segment.get(3).strip(),
                )

        elif tag == "DMG":
            subscriber = _replace_subscriber(
                subscriber,
                date_of_birth=optional_date(segment.get(2), field_name="DMG02", segment=segment),
                sex=segment.get(3).strip(),
            )

        elif tag == "REF":
            qualifier = segment.get(1).strip()
            value = segment.get(2).strip()
            if qualifier == "EI" and section == _Section.billing:
                billing = _replace_billing(billing, tax_id=value)
            elif claim is not None:
                if qualifier == "G1":
                    claim.prior_auth_number = value
                elif qualifier == "9F":
                    claim.referral_number = value
                elif qualifier == "F8":
                    # PD-7. Losing this makes three submissions of one claim
                    # look like three claims.
                    claim.original_claim_id = value

        elif tag == "CLM":
            close_line()
            section = _Section.claim
            composite = segment.composite(5, separators)
            claim = _ClaimBuilder(
                claim_id=segment.get(1).strip(),
                total_charge=parse_amount(segment.get(2), field_name="CLM02", segment=segment),
                place_of_service=composite[0].strip() if composite else "",
                claim_frequency_code=(composite[2].strip() if len(composite) > 2 else "1"),
                billing_provider=billing,
                subscriber=subscriber,
                payer_id=payer_id,
                payer_name=payer_name,
                claim_filing_code=claim_filing_code,
                payer_responsibility=payer_responsibility,
            )
            claims.append(claim)

        elif tag == "HI" and claim is not None:
            _read_diagnoses(segment, separators, claim)

        elif tag == "LX":
            close_line()
            section = _Section.line
            number = segment.get(1).strip()
            if not number.isdigit():
                raise MalformedX12Error(
                    reason="service line number is not a number",
                    field="LX01",
                    segment=str(segment),
                )
            line = _LineBuilder(line_number=int(number))

        elif tag == "SV1":
            if line is None:
                raise MalformedX12Error(
                    reason="SV1 service line outside any LX loop",
                    field="SV1",
                    segment=str(segment),
                )
            procedure = segment.composite(1, separators)
            if len(procedure) < 2:
                raise MalformedX12Error(
                    reason="SV101 does not carry a qualifier and a procedure code",
                    field="SV101",
                    segment=str(segment),
                )
            line.procedure_code = procedure[1].strip()
            line.modifiers = tuple(
                m.strip() for m in procedure[2 : 2 + _SV1_MAX_MODIFIERS] if m.strip()
            )
            line.charge_amount = parse_amount(segment.get(2), field_name="SV102", segment=segment)
            line.unit_basis = segment.get(3).strip() or "UN"
            units = segment.get(4).strip()
            line.units = (
                parse_quantity(units, field_name="SV104", segment=segment)
                if units
                else Decimal("1")
            )
            line.diagnosis_pointers = _parse_integer_pointers(
                segment.composite(7, separators), segment
            )

        elif tag == "DTP":
            qualifier = segment.get(1).strip()
            value = segment.get(3)
            if qualifier == "472" and line is not None:
                line.service_date = parse_date(value, field_name="DTP03", segment=segment)
            elif qualifier == "434" and claim is not None:
                # D8 is a single date; RD8 is a range, whose start is the date
                # of service for our purposes.
                claim.service_date = parse_date(
                    value.split("-")[0], field_name="DTP03", segment=segment
                )

    close_line()

    return ClaimSubmission(
        control_number=transaction.control_number,
        submitter=submitter,
        receiver=receiver,
        claims=tuple(c.build() for c in claims),
        created_on=created_on,
        created_time=created_time,
        implementation_reference=transaction.implementation_reference or "005010X222A1",
    )


def _read_diagnoses(segment: Segment, separators: Separators, claim: _ClaimBuilder) -> None:
    """HI carries the principal diagnosis first, then secondaries.

    Order is preserved because the diagnosis pointers on every service line are
    one-based indexes into this list. Sorting it, or de-duplicating it, silently
    repoints every line on the claim at a different diagnosis.
    """
    for index in range(1, _MAX_DIAGNOSES + 1):
        components = segment.composite(index, separators)
        if not components:
            continue
        qualifier = components[0].strip()
        if qualifier not in _PRINCIPAL_DIAGNOSIS and qualifier not in _SECONDARY_DIAGNOSIS:
            continue
        if len(components) < 2:
            raise MalformedX12Error(
                reason="diagnosis composite carries a qualifier with no code",
                field=f"HI{index:02d}",
                segment=str(segment),
            )
        claim.diagnoses.append(components[1].strip())


def _replace_billing(current: BillingProvider, **changes: object) -> BillingProvider:
    """`dataclasses.replace` with the typing the strict checker wants."""
    values: dict[str, object] = {
        "name": current.name,
        "npi": current.npi,
        "taxonomy_code": current.taxonomy_code,
        "address": current.address,
        "city": current.city,
        "state": current.state,
        "postal_code": current.postal_code,
        "tax_id": current.tax_id,
    }
    values.update(changes)
    return BillingProvider(**values)  # type: ignore[arg-type]


def _replace_subscriber(current: Subscriber, **changes: object) -> Subscriber:
    values: dict[str, object] = {
        "last_name": current.last_name,
        "first_name": current.first_name,
        "member_id": current.member_id,
        "address": current.address,
        "city": current.city,
        "state": current.state,
        "postal_code": current.postal_code,
        "date_of_birth": current.date_of_birth,
        "sex": current.sex,
    }
    values.update(changes)
    return Subscriber(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _amount(value: Decimal) -> str:
    """Render money the way X12 carries it: two places, no thousands marks."""
    return str(value.quantize(_TWO_PLACES))


def _quantity(value: Decimal) -> str:
    """Render a quantity without a trailing `.00` on whole units."""
    normalised = value.normalize()
    text = format(normalised, "f")
    return text


def _d8(value: date | None) -> str:
    return value.strftime("%Y%m%d") if value is not None else ""


class _Composed(str):
    """A value already assembled from components.

    Marked so `_SegmentWriter.add` does not re-check it and find the component
    separator it just put there. Checking the components on the way in is the
    point at which data and delimiter are still distinguishable; after joining
    they are not.
    """

    __slots__ = ()


class _SegmentWriter:
    """Accumulates segments and renders them with the standard delimiters."""

    def __init__(self, separators: Separators) -> None:
        self._separators = separators
        self._segments: list[str] = []
        self._delimiters = frozenset(
            {
                separators.element,
                separators.component,
                separators.repetition,
                separators.segment,
            }
        )

    def _check(self, value: str) -> str:
        """Refuse data carrying a delimiter.

        X12 has no escape mechanism: a `*` inside a patient's surname ends the
        element, and every element after it shifts by one. The file stays
        syntactically valid, so nothing downstream raises -- the claim simply
        arrives at the payer with the address in the date field. Senders are
        required to strip delimiters before composing, and the only two options
        here are to do that silently or to refuse.

        Refusing, because stripping alters a name or an address to make a file
        well formed, and a patient whose surname is quietly rewritten is a
        worse outcome than a submission that stops and says why.
        """
        found = self._delimiters.intersection(value)
        if found:
            raise MalformedX12Error(
                reason=(
                    f"value contains an X12 delimiter and cannot be represented: {sorted(found)}"
                ),
                field="generated element",
                segment=value[:40],
            )
        return value

    def add(self, tag: str, *elements: str) -> None:
        # Trailing empty elements are dropped, which is what X12 permits and
        # what every real sender does. Keeping them would make a generated file
        # differ cosmetically from the same claim sent by anyone else.
        values = [
            element if isinstance(element, _Composed) else self._check(element)
            for element in elements
        ]
        while values and values[-1] == "":
            values.pop()
        parts = [tag, *values]
        self._segments.append(self._separators.element.join(parts))

    def composite(self, *components: str) -> _Composed:
        values = [self._check(component) for component in components]
        while values and values[-1] == "":
            values.pop()
        return _Composed(self._separators.component.join(values))

    def count(self) -> int:
        return len(self._segments)

    def render(self) -> str:
        terminator = self._separators.segment
        return "".join(segment + terminator for segment in self._segments)


def _isa(
    *,
    sender: str,
    receiver: str,
    control_number: str,
    created_on: date,
    created_time: str,
    usage_indicator: str,
    separators: Separators,
) -> str:
    """Build the one fixed-width segment in X12.

    Every delimiter in the file is read back out of this segment by offset --
    element at index 3, repetition at 82, component at 104, terminator at 105
    -- so it is assembled from parts of known width rather than formatted. A
    single character of drift here makes the whole file unreadable, and the
    length assertion below is what catches that at the point of the mistake
    rather than in the tokenizer.
    """
    element = separators.element
    fields = [
        "00",
        " " * 10,
        "00",
        " " * 10,
        "ZZ",
        sender[:15].ljust(15),
        "ZZ",
        receiver[:15].ljust(15),
        created_on.strftime("%y%m%d"),
        created_time[:4].rjust(4, "0"),
        separators.repetition,
        "00501",
        control_number[:9].rjust(9, "0"),
        "0",
        usage_indicator[:1] or "T",
        separators.component,
    ]
    isa = "ISA" + element + element.join(fields) + separators.segment
    expected = 106
    if len(isa) != expected:
        raise MalformedX12Error(
            reason=f"generated ISA is {len(isa)} characters, not {expected}",
            field="ISA",
            segment="ISA",
        )
    return isa


def build_837(submission: ClaimSubmission, *, separators: Separators | None = None) -> str:
    """Serialise claims as an 837P interchange.

    Claims are grouped into billing provider loops as they arrive, which is how
    real batching works and what makes the round trip exact: a parser reads the
    billing provider from the enclosing 2000A loop, so a generator that emitted
    one loop per file would move every claim onto the first claim's provider.

    Nothing here supplies a code the caller did not. In particular no modifier
    is ever added (PD-16) and no diagnosis pointer is invented -- a line that
    arrives without one is written without one, so that the validator sees the
    claim the caller actually built.
    """
    separators = separators or STANDARD_SEPARATORS
    writer = _SegmentWriter(separators)
    created_on = submission.created_on or date.today()  # noqa: DTZ011

    writer_isa = _isa(
        sender=submission.submitter.etin,
        receiver=submission.receiver.etin,
        control_number=submission.control_number,
        created_on=created_on,
        created_time=submission.created_time,
        usage_indicator=submission.usage_indicator,
        separators=separators,
    )

    writer.add(
        "GS",
        "HC",
        submission.submitter.etin,
        submission.receiver.etin,
        created_on.strftime("%Y%m%d"),
        submission.created_time,
        submission.control_number.lstrip("0") or "1",
        "X",
        submission.implementation_reference,
    )

    st_index = writer.count()
    writer.add("ST", "837", submission.control_number, submission.implementation_reference)
    writer.add(
        "BHT",
        "0019",
        "00",
        submission.control_number,
        _d8(created_on),
        submission.created_time,
        "CH",
    )

    writer.add(
        "NM1", "41", "2", submission.submitter.name, "", "", "", "", "46", submission.submitter.etin
    )
    if submission.submitter.contact_name or submission.submitter.contact_phone:
        writer.add(
            "PER",
            "IC",
            submission.submitter.contact_name,
            "TE",
            submission.submitter.contact_phone,
        )
    writer.add(
        "NM1", "40", "2", submission.receiver.name, "", "", "", "", "46", submission.receiver.etin
    )

    hierarchical_id = 0
    index = 0
    while index < len(submission.claims):
        provider = submission.claims[index].billing_provider
        hierarchical_id += 1
        billing_id = hierarchical_id
        writer.add("HL", str(billing_id), "", _HL_BILLING_PROVIDER, "1")
        if provider.taxonomy_code:
            writer.add("PRV", "BI", "PXC", provider.taxonomy_code)
        writer.add("NM1", "85", "2", provider.name, "", "", "", "", "XX", provider.npi)
        if provider.address:
            writer.add("N3", provider.address)
        if provider.city or provider.state or provider.postal_code:
            writer.add("N4", provider.city, provider.state, provider.postal_code)
        if provider.tax_id:
            writer.add("REF", "EI", provider.tax_id)

        while index < len(submission.claims) and (
            submission.claims[index].billing_provider == provider
        ):
            hierarchical_id += 1
            _write_claim(
                writer,
                submission.claims[index],
                hierarchical_id=hierarchical_id,
                parent_id=billing_id,
            )
            index += 1

    # SE counts itself, so the transaction is everything after ST plus both.
    segment_count = writer.count() - st_index + 1
    writer.add("SE", str(segment_count), submission.control_number)
    writer.add("GE", "1", submission.control_number.lstrip("0") or "1")
    writer.add("IEA", "1", submission.control_number[:9].rjust(9, "0"))

    return writer_isa + writer.render()


def _write_claim(
    writer: _SegmentWriter, claim: Claim837, *, hierarchical_id: int, parent_id: int
) -> None:
    """One 2000B subscriber loop and the 2300 claim inside it."""
    writer.add("HL", str(hierarchical_id), str(parent_id), _HL_SUBSCRIBER, "0")
    writer.add(
        "SBR",
        claim.payer_responsibility,
        "18",
        "",
        "",
        "",
        "",
        "",
        "",
        claim.claim_filing_code,
    )

    subscriber = claim.subscriber
    writer.add(
        "NM1",
        "IL",
        "1",
        subscriber.last_name,
        subscriber.first_name,
        "",
        "",
        "",
        "MI",
        subscriber.member_id,
    )
    if subscriber.address:
        writer.add("N3", subscriber.address)
    if subscriber.city or subscriber.state or subscriber.postal_code:
        writer.add("N4", subscriber.city, subscriber.state, subscriber.postal_code)
    if subscriber.date_of_birth is not None or subscriber.sex:
        writer.add("DMG", "D8", _d8(subscriber.date_of_birth), subscriber.sex)

    writer.add("NM1", "PR", "2", claim.payer_name, "", "", "", "", "PI", claim.payer_id)

    writer.add(
        "CLM",
        claim.claim_id,
        _amount(claim.total_charge),
        "",
        "",
        writer.composite(claim.place_of_service, "B", claim.claim_frequency_code),
        "Y",
        "A",
        "Y",
        "Y",
    )
    if claim.service_date is not None:
        writer.add("DTP", "434", "D8", _d8(claim.service_date))
    if claim.prior_auth_number:
        writer.add("REF", "G1", claim.prior_auth_number)
    if claim.referral_number:
        writer.add("REF", "9F", claim.referral_number)
    if claim.original_claim_id:
        # PD-7: the chain to the claim this one replaces.
        writer.add("REF", "F8", claim.original_claim_id)

    if claim.diagnoses:
        elements = [writer.composite("ABK", claim.diagnoses[0])]
        elements.extend(writer.composite("ABF", code) for code in claim.diagnoses[1:_MAX_DIAGNOSES])
        writer.add("HI", *elements)

    for line in claim.lines:
        writer.add("LX", str(line.line_number))
        writer.add(
            "SV1",
            writer.composite("HC", line.procedure_code, *line.modifiers),
            _amount(line.charge_amount),
            line.unit_basis,
            _quantity(line.units),
            "",
            "",
            writer.composite(*(str(p) for p in line.diagnosis_pointers)),
        )
        if line.service_date is not None:
            writer.add("DTP", "472", "D8", _d8(line.service_date))
