"""835 remittance advice parser (M1.2).

This is the segment of the project where a quiet bug is most expensive.
CLAUDE.md section 5 says so outright -- *"Parser bugs corrupt training data
silently and permanently. Slow down here."* -- and the reason is that an 835 is
the only record of why a payer did what it did. Lose a reason code here and no
later stage can tell that anything is missing; the claim simply appears to have
been adjusted for one reason instead of three, and every denial rate, every
feature and every model trained on it is quietly wrong.

Three decisions this module exists to get right.

**CAS segments are unrolled, never collapsed (PD-6).** One `CAS` segment
carries a group code and up to six reason/amount/quantity triplets, and one
service line can carry several CAS segments -- one per group. The natural thing
to write produces one row per segment. The correct thing produces one row per
*reason*, which is the grain the analytical layer needs and the grain that
makes `CO-45` and `PR-45` distinguishable. DOC-02 BR-DATA-02 calls collapsing
them the most common way this data is destroyed.

**A reason code never travels without its group code (PD-3).** `Adjustment`
has no default and no optional group: it is structurally impossible to
construct one without saying whether the amount is a contractual write-off the
patient may never be billed for (`CO`) or patient responsibility (`PR`). The
same number, `45`, means opposite things under the two.

**Money is `Decimal`, parsed strictly.** A float here is a rounding error in a
ledger. An unparseable amount raises rather than defaulting to zero, because a
zero that should have been 4,312.80 balances nothing and is invisible.

The balance assertion itself lives here as a computed property rather than an
exception: this module's job is to report what the file says, and deciding that
an unbalanced file must be quarantined is the loader's (M1.3). Keeping the two
apart means a reconciliation tool can read a broken file deliberately, which is
exactly what someone will need to do the first time a payer sends one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

from rcm.ingestion.x12.envelope import TransactionSet
from rcm.ingestion.x12.fields import (
    ZERO as _ZERO,
)
from rcm.ingestion.x12.fields import (
    optional_amount as _optional_amount,
)
from rcm.ingestion.x12.fields import (
    parse_amount,
    parse_date,
)
from rcm.ingestion.x12.tokenizer import Segment, Separators
from rcm.platform.errors import MalformedX12Error

#: The five CAS group codes (X12 code list 1033). This set is closed, and a
#: sixth value means the file is not an 835 we understand -- not that we should
#: invent a meaning for it.
#:
#:   CO  contractual obligation   -- provider writes off, never billable to the patient (PD-19)
#:   PR  patient responsibility   -- deductible, coinsurance, copay
#:   OA  other adjustment
#:   PI  payer initiated reduction
#:   CR  correction and reversal
VALID_CAS_GROUPS: Final[frozenset[str]] = frozenset({"CO", "PR", "OA", "PI", "CR"})

#: Tolerance for balance comparisons. X12 money is two decimal places, and
#: payers round cents inconsistently across a claim; a cent either way is not a
#: broken file. Anything larger is.
BALANCE_TOLERANCE: Final = Decimal("0.02")

#: CAS triplets start at element 2 and repeat every three elements.
_CAS_FIRST_TRIPLET: Final = 2
_CAS_TRIPLET_WIDTH: Final = 3
#: 835 permits six triplets per CAS segment.
_CAS_MAX_TRIPLETS: Final = 6


@dataclass(frozen=True, slots=True)
class Adjustment:
    """One reason a payer did not pay an amount in full.

    One row per reason, which is the whole point. `group` has no default
    because PD-3 forbids a CARC that travels without one.
    """

    group: str
    reason_code: str
    amount: Decimal
    #: CAS quantity, present on unit-based adjustments. Rarely populated.
    quantity: Decimal | None = None
    #: True when this adjustment was recorded against the claim rather than a
    #: service line. Claim-level CAS is legal and uncommon, and conflating the
    #: two double-counts the money.
    claim_level: bool = False


@dataclass(frozen=True, slots=True)
class ServiceLine:
    """Loop 2110: what the payer did with one service line."""

    #: SVC01 component 2. The procedure actually adjudicated, which is not
    #: always the one submitted -- a payer may bundle or re-code.
    procedure_code: str
    modifiers: tuple[str, ...]
    charge_amount: Decimal
    paid_amount: Decimal
    units: Decimal
    #: AMT*B6. The allowed amount, absent on some payers' files.
    allowed_amount: Decimal | None
    adjustments: tuple[Adjustment, ...]
    #: LQ*HE remark codes, which qualify the reason codes.
    remark_codes: tuple[str, ...]
    service_date: date | None

    @property
    def adjustment_total(self) -> Decimal:
        return sum((a.amount for a in self.adjustments), _ZERO)

    @property
    def balances(self) -> bool:
        """SVC02 == SVC03 + sum(CAS) -- the line's own integrity statement."""
        return abs(self.charge_amount - (self.paid_amount + self.adjustment_total)) <= (
            BALANCE_TOLERANCE
        )

    @property
    def is_denied(self) -> bool:
        """Nothing was paid and something was adjusted.

        Deliberately not "paid == 0": a line can legitimately pay zero when the
        whole allowed amount went to patient responsibility, which is a PR
        adjustment and not a denial. The distinction is PD-4's territory and is
        refined in M4.8; this is the structural reading the parser can make.
        """
        return self.paid_amount == _ZERO and bool(self.adjustments)


@dataclass(frozen=True, slots=True)
class ClaimPayment:
    """Loop 2100: what the payer did with one claim."""

    #: CLP01, the claim identifier as *the provider* submitted it.
    claim_id: str
    #: CLP02 status: 1 processed as primary, 4 denied, 22 reversal of a
    #: previous payment, and so on.
    status_code: str
    total_charge: Decimal
    total_paid: Decimal
    patient_responsibility: Decimal
    filing_indicator: str
    #: CLP07, the payer's own control number -- the handle needed to ask the
    #: payer about this claim, and the only key that survives a resubmission.
    payer_control_number: str
    service_lines: tuple[ServiceLine, ...]
    claim_adjustments: tuple[Adjustment, ...]
    patient_last_name: str = ""
    patient_first_name: str = ""
    member_id: str = ""
    rendering_provider_npi: str = ""
    service_date: date | None = None

    @property
    def adjustments(self) -> tuple[Adjustment, ...]:
        """Every adjustment on the claim, claim-level and line-level alike."""
        return self.claim_adjustments + tuple(
            adjustment for line in self.service_lines for adjustment in line.adjustments
        )

    @property
    def is_reversal(self) -> bool:
        """CLP02 = 22: the payer is taking back a payment it already made.

        A reversal is not a new claim and must reopen the original (PD-7,
        M1.10). Treating it as one inflates both claim counts and denial rates.
        """
        return self.status_code.strip() == "22"

    @property
    def is_denied(self) -> bool:
        """CLP02 = 4: denied. Distinct from a rejection, which never got here (PD-4)."""
        return self.status_code.strip() == "4"


@dataclass(frozen=True, slots=True)
class ProviderAdjustment:
    """A PLB entry: money moved outside any single claim.

    Recoupments, interest, and forwarding balances. These are what make a file
    fail to balance for a parser that ignores them, which is why the generator
    emits them deliberately.

    Sign convention is the standard's and is counter-intuitive: a **positive**
    PLB amount *reduces* the payment. A recoupment of 143.89 appears as
    `+143.89` and the check is 143.89 smaller.
    """

    provider_id: str
    fiscal_period_end: date | None
    reason_code: str
    reference: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Remittance:
    """One parsed 835 transaction set."""

    #: BPR02, the total of the payment this file accompanies.
    payment_amount: Decimal
    #: BPR16 or DTM*405: the date of the payment.
    payment_date: date | None
    #: TRN02, the reassociation trace number -- what ties this file to the
    #: deposit that appears on the bank statement.
    trace_number: str
    payer_name: str
    #: PD-8: the payer's *identifier*, which is what identity keys on. A name
    #: is not an identifier -- the same payer arrives as "BCBS TX", "Blue Cross
    #: Texas" and "BCBSTX", and keying on the text produces three payers with
    #: three denial rates, each internally consistent and all wrong.
    #:
    #: Taken in order of authority: REF*2U in the payer loop is the payer
    #: identification number proper; REF*EV in the header is the identifier the
    #: sender used for reassociation; TRN03 is the originating company
    #: identifier, which every 835 carries. Empty only if a file has none of
    #: the three, in which case the caller falls back to the interchange
    #: sender.
    payer_identifier: str
    payee_name: str
    payee_npi: str
    claims: tuple[ClaimPayment, ...]
    provider_adjustments: tuple[ProviderAdjustment, ...]
    transaction_control_number: str = ""
    _unknown_cas_groups: tuple[str, ...] = field(default=(), repr=False)

    @property
    def claim_payment_total(self) -> Decimal:
        return sum((c.total_paid for c in self.claims), _ZERO)

    @property
    def provider_adjustment_total(self) -> Decimal:
        return sum((p.amount for p in self.provider_adjustments), _ZERO)

    @property
    def expected_payment(self) -> Decimal:
        """What BPR02 should be, from the file's own contents.

        Claim payments less provider-level adjustments. The subtraction is the
        sign convention described on `ProviderAdjustment`.
        """
        return self.claim_payment_total - self.provider_adjustment_total

    @property
    def balance_discrepancy(self) -> Decimal:
        return self.expected_payment - self.payment_amount

    @property
    def balances(self) -> bool:
        """IR-02 / BR-M9-01: the file reconciles against its own stated total."""
        return abs(self.balance_discrepancy) <= BALANCE_TOLERANCE

    @property
    def unbalanced_service_lines(self) -> tuple[tuple[str, str], ...]:
        """(claim_id, procedure_code) for every line that does not reconcile."""
        return tuple(
            (claim.claim_id, line.procedure_code)
            for claim in self.claims
            for line in claim.service_lines
            if not line.balances
        )

    @property
    def adjustments(self) -> tuple[Adjustment, ...]:
        return tuple(a for claim in self.claims for a in claim.adjustments)


def _parse_cas(segment: Segment) -> list[Adjustment]:
    """Unroll one CAS segment into one `Adjustment` per reason (PD-6).

    CAS01 is the group code; elements 2 onward are reason/amount/quantity
    triplets, up to six. Trailing triplets are omitted rather than padded, and
    a quantity is frequently empty while the reason and amount are not -- which
    is why the stride is fixed at three rather than inferred from how many
    elements happen to be present.
    """
    group = segment.get(1).strip()
    if group not in VALID_CAS_GROUPS:
        raise MalformedX12Error(
            reason="CAS group code is not one of CO, PR, OA, PI, CR",
            assertion="CAS01 in code list 1033",
            found=group,
            segment=str(segment),
        )

    adjustments: list[Adjustment] = []
    for triplet in range(_CAS_MAX_TRIPLETS):
        base = _CAS_FIRST_TRIPLET + triplet * _CAS_TRIPLET_WIDTH
        reason = segment.get(base).strip()
        if not reason:
            # No further triplets. X12 drops trailing empty elements, so the
            # first absent reason code ends the segment.
            break
        amount_raw = segment.get(base + 1)
        quantity_raw = segment.get(base + 2).strip()
        adjustments.append(
            Adjustment(
                group=group,
                reason_code=reason,
                amount=parse_amount(amount_raw, field_name=f"CAS{base + 1:02d}", segment=segment),
                quantity=(
                    parse_amount(quantity_raw, field_name=f"CAS{base + 2:02d}", segment=segment)
                    if quantity_raw
                    else None
                ),
            )
        )

    if not adjustments:
        raise MalformedX12Error(
            reason="CAS segment carries a group code but no reason",
            segment=str(segment),
        )
    return adjustments


def _parse_plb(segment: Segment, separators: Separators) -> list[ProviderAdjustment]:
    """Read a PLB segment's repeating reason/amount pairs.

    PLB01 is the provider, PLB02 the fiscal period end, and from element 3
    onward the segment repeats: a composite `reason:reference` followed by an
    amount.
    """
    provider_id = segment.get(1).strip()
    period_end: date | None = None
    if segment.get(2).strip():
        period_end = parse_date(segment.get(2), field_name="PLB02", segment=segment)

    entries: list[ProviderAdjustment] = []
    index = 3
    while segment.get(index).strip():
        components = segment.composite(index, separators)
        amount_raw = segment.get(index + 1)
        if not amount_raw.strip():
            raise MalformedX12Error(
                reason="PLB adjustment has a reason but no amount",
                field=f"PLB{index + 1:02d}",
                segment=str(segment),
            )
        entries.append(
            ProviderAdjustment(
                provider_id=provider_id,
                fiscal_period_end=period_end,
                reason_code=components[0] if components else "",
                reference=components[1] if len(components) > 1 else "",
                amount=parse_amount(amount_raw, field_name=f"PLB{index + 1:02d}", segment=segment),
            )
        )
        index += 2
    return entries


@dataclass
class _ClaimBuilder:
    """Mutable accumulator for one CLP loop, frozen into a `ClaimPayment` at its end."""

    claim_id: str
    status_code: str
    total_charge: Decimal
    total_paid: Decimal
    patient_responsibility: Decimal
    filing_indicator: str
    payer_control_number: str
    lines: list[_LineBuilder] = field(default_factory=list)
    claim_adjustments: list[Adjustment] = field(default_factory=list)
    patient_last_name: str = ""
    patient_first_name: str = ""
    member_id: str = ""
    rendering_provider_npi: str = ""
    service_date: date | None = None

    def build(self) -> ClaimPayment:
        return ClaimPayment(
            claim_id=self.claim_id,
            status_code=self.status_code,
            total_charge=self.total_charge,
            total_paid=self.total_paid,
            patient_responsibility=self.patient_responsibility,
            filing_indicator=self.filing_indicator,
            payer_control_number=self.payer_control_number,
            service_lines=tuple(line.build() for line in self.lines),
            claim_adjustments=tuple(self.claim_adjustments),
            patient_last_name=self.patient_last_name,
            patient_first_name=self.patient_first_name,
            member_id=self.member_id,
            rendering_provider_npi=self.rendering_provider_npi,
            service_date=self.service_date,
        )


@dataclass
class _LineBuilder:
    """Mutable accumulator for one SVC loop."""

    procedure_code: str
    modifiers: tuple[str, ...]
    charge_amount: Decimal
    paid_amount: Decimal
    units: Decimal
    allowed_amount: Decimal | None = None
    adjustments: list[Adjustment] = field(default_factory=list)
    remark_codes: list[str] = field(default_factory=list)
    service_date: date | None = None

    def build(self) -> ServiceLine:
        return ServiceLine(
            procedure_code=self.procedure_code,
            modifiers=self.modifiers,
            charge_amount=self.charge_amount,
            paid_amount=self.paid_amount,
            units=self.units,
            allowed_amount=self.allowed_amount,
            adjustments=tuple(self.adjustments),
            remark_codes=tuple(self.remark_codes),
            service_date=self.service_date,
        )


def parse_835(transaction: TransactionSet, separators: Separators) -> Remittance:
    """Parse one 835 transaction set into a `Remittance`.

    Loop membership is decided by position: a CAS segment belongs to the SVC
    that precedes it, or to the CLP if no SVC has opened yet. That is how the
    standard defines it and it is why the claim- and line-level cursors below
    are reset in the order they are.
    """
    if transaction.identifier.strip() != "835":
        raise MalformedX12Error(
            reason="transaction set is not an 835",
            assertion="ST01 == 835",
            found=transaction.identifier,
        )

    payment_amount: Decimal | None = None
    payment_date: date | None = None
    trace_number = ""
    payer_name = ""
    payee_name = ""
    payee_npi = ""
    #: Collected separately and resolved by precedence at the end, because the
    #: segments arrive in whatever order the sender chose.
    payer_reference_2u = ""
    payer_reference_ev = ""
    originating_company_id = ""

    claims: list[_ClaimBuilder] = []
    claim: _ClaimBuilder | None = None
    line: _LineBuilder | None = None
    provider_adjustments: list[ProviderAdjustment] = []
    #: Which N1 loop is open: "PR" payer, "PE" payee.
    party = ""

    for segment in transaction.segments:
        tag = segment.tag

        if tag == "BPR":
            payment_amount = parse_amount(segment.get(2), field_name="BPR02", segment=segment)
            if segment.get(16).strip():
                payment_date = parse_date(segment.get(16), field_name="BPR16", segment=segment)

        elif tag == "TRN":
            trace_number = segment.get(2).strip()
            originating_company_id = segment.get(3).strip()

        elif tag == "REF" and claim is None:
            qualifier = segment.get(1).strip()
            if qualifier == "2U" and party == "PR":
                payer_reference_2u = segment.get(2).strip()
            elif qualifier == "EV" and not payer_reference_ev:
                payer_reference_ev = segment.get(2).strip()

        elif tag == "DTM" and claim is None and line is None:
            # DTM*405 in the header is the production date of the payment, and
            # is the fallback when BPR16 is absent.
            if segment.get(1).strip() == "405" and payment_date is None:
                payment_date = parse_date(segment.get(2), field_name="DTM02", segment=segment)

        elif tag == "N1":
            party = segment.get(1).strip()
            if party == "PR":
                payer_name = segment.get(2).strip()
            elif party == "PE":
                payee_name = segment.get(2).strip()
                if segment.get(3).strip() == "XX":
                    payee_npi = segment.get(4).strip()

        elif tag == "CLP":
            claim = _ClaimBuilder(
                claim_id=segment.get(1).strip(),
                status_code=segment.get(2).strip(),
                total_charge=parse_amount(segment.get(3), field_name="CLP03", segment=segment),
                total_paid=parse_amount(segment.get(4), field_name="CLP04", segment=segment),
                patient_responsibility=_optional_amount(
                    segment.get(5), field_name="CLP05", segment=segment
                ),
                filing_indicator=segment.get(6).strip(),
                payer_control_number=segment.get(7).strip(),
            )
            claims.append(claim)
            # A new claim closes any open service line. Without this reset the
            # first CAS of the next claim would attach to the previous claim's
            # last line.
            line = None

        elif tag == "SVC" and claim is not None:
            components = segment.composite(1, separators)
            if len(components) < 2:
                raise MalformedX12Error(
                    reason="SVC01 does not carry a qualifier and a procedure code",
                    field="SVC01",
                    segment=str(segment),
                )
            line = _LineBuilder(
                procedure_code=components[1].strip(),
                modifiers=tuple(c.strip() for c in components[2:] if c.strip()),
                charge_amount=parse_amount(segment.get(2), field_name="SVC02", segment=segment),
                paid_amount=parse_amount(segment.get(3), field_name="SVC03", segment=segment),
                units=_optional_amount(segment.get(5), field_name="SVC05", segment=segment)
                or Decimal("1"),
            )
            claim.lines.append(line)

        elif tag == "CAS":
            unrolled = _parse_cas(segment)
            if line is not None:
                line.adjustments.extend(unrolled)
            elif claim is not None:
                claim.claim_adjustments.extend(
                    Adjustment(
                        group=a.group,
                        reason_code=a.reason_code,
                        amount=a.amount,
                        quantity=a.quantity,
                        claim_level=True,
                    )
                    for a in unrolled
                )
            else:
                raise MalformedX12Error(
                    reason="CAS segment appears outside any claim",
                    segment=str(segment),
                )

        elif tag == "AMT" and line is not None and segment.get(1).strip() == "B6":
            line.allowed_amount = parse_amount(segment.get(2), field_name="AMT02", segment=segment)

        elif tag == "LQ" and line is not None and segment.get(1).strip() == "HE":
            remark = segment.get(2).strip()
            if remark:
                line.remark_codes.append(remark)

        elif tag == "NM1" and claim is not None:
            qualifier = segment.get(1).strip()
            if qualifier == "QC":
                claim.patient_last_name = segment.get(3).strip()
                claim.patient_first_name = segment.get(4).strip()
                if segment.get(8).strip() == "MI":
                    claim.member_id = segment.get(9).strip()
            elif qualifier == "82" and segment.get(8).strip() == "XX":
                claim.rendering_provider_npi = segment.get(9).strip()

        elif tag == "DTM" and line is not None and segment.get(1).strip() == "472":
            line.service_date = parse_date(segment.get(2), field_name="DTM02", segment=segment)

        elif tag == "DTM" and claim is not None and segment.get(1).strip() in ("232", "233"):
            if claim.service_date is None:
                claim.service_date = parse_date(segment.get(2), field_name="DTM02", segment=segment)

        elif tag == "PLB":
            # PLB sits after every claim, so it closes the current loops.
            claim = None
            line = None
            provider_adjustments.extend(_parse_plb(segment, separators))

    if payment_amount is None:
        raise MalformedX12Error(
            reason="835 has no BPR segment, so it states no payment total",
            assertion="BPR present",
            transaction_control=transaction.control_number,
        )

    return Remittance(
        payment_amount=payment_amount,
        payment_date=payment_date,
        trace_number=trace_number,
        payer_name=payer_name,
        payer_identifier=(payer_reference_2u or payer_reference_ev or originating_company_id),
        payee_name=payee_name,
        payee_npi=payee_npi,
        claims=tuple(c.build() for c in claims),
        provider_adjustments=tuple(provider_adjustments),
        transaction_control_number=transaction.control_number,
    )
