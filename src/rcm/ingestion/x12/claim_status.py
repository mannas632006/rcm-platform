"""277CA and 277: did the payer accept the claim, and where is it (M1.6).

This is where PD-4 lives. DOC-02 section 2.5 spends a page on the distinction
and it is worth restating, because the two look alike and behave oppositely:

    |                       | Rejection            | Denial            |
    | reached adjudication? | no                   | yes               |
    | arrives via           | 999 / 277CA          | 835               |
    | timely filing         | clock keeps running  | date preserved    |
    | appealable?           | no -- fix and resend | yes               |
    | counts in denial rate | **no**               | yes               |

    BR-GLOBAL-02 (MUST): Rejections and denials MUST be stored and reported
    as distinct outcome types. Conflating them inflates the denial rate and
    hides front-end data quality problems.

A 277CA is the payer's front-end verdict: the claim arrived, and either it
entered adjudication or it was returned unprocessable. A 277 is the same
segment vocabulary answering a status enquiry later in the claim's life. One
parser reads both, because the thing being read -- the `STC` status composite
-- is identical; what differs is when it arrives and what the platform does
with it.

**The status is a composite, and the first component is the one that
decides.** `STC01-1` is the category (X12 code list 507): `A1` and `A2` are
acknowledgement and acceptance, `A3`, `A4`, `A6`, `A7` and `A8` are the
payer returning the claim unprocessed. Reading only `STC01-2`, the detailed
status code, gives a number with no verdict attached -- the same code means
accepted or rejected depending on the category it arrived under.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

from rcm.ingestion.x12.envelope import TransactionSet
from rcm.ingestion.x12.fields import optional_amount, optional_date
from rcm.ingestion.x12.tokenizer import Separators
from rcm.platform.errors import MalformedX12Error

#: STC01-1 health care claim status category codes (X12 code list 507) that
#: mean the claim is progressing.
#:
#:   A1  acknowledgement / receipt -- the claim arrived
#:   A2  acknowledgement / acceptance into adjudication
ACCEPTED_CATEGORIES: Final[frozenset[str]] = frozenset({"A1", "A2"})

#: Categories that mean the claim was returned without being adjudicated.
#: Every one of these is a *rejection* in DOC-02 section 2.5's sense.
#:
#:   A3  returned as unprocessable
#:   A4  not found
#:   A6  rejected, missing information
#:   A7  rejected, invalid information
#:   A8  rejected, relational field in error
REJECTED_CATEGORIES: Final[frozenset[str]] = frozenset({"A3", "A4", "A6", "A7", "A8"})

#: HL03 levels in a 277: 20 information source, 21 receiver, 19 provider,
#: 22 subscriber, 23 dependent. The claim statuses hang below the patient.
_HL_SUBSCRIBER: Final = "22"
_HL_DEPENDENT: Final = "23"


@dataclass(frozen=True, slots=True)
class ClaimStatus:
    """One STC: what the payer says about one claim."""

    #: TRN02 in loop 2200D, the claim control number the submitter sent --
    #: which is how this verdict is matched back to a claim.
    claim_id: str
    #: STC01-1, the category. This is the element that decides the outcome.
    category_code: str
    #: STC01-2, the detailed status code within that category.
    status_code: str = ""
    #: STC01-3, the entity the status is about, where the payer names one.
    entity_code: str = ""
    effective_date: date | None = None
    #: STC03 action code, and STC04/05 the amounts the payer is working from.
    action_code: str = ""
    total_charge: Decimal | None = None
    #: STC12, free-form text. Often the only human-readable reason, and the
    #: first thing an operator reads.
    description: str = ""
    #: REF*1K, the payer's own claim number, where the 277 carries one.
    payer_claim_number: str = ""
    #: The patient the claim is for, as the 277 names them.
    patient_last_name: str = ""
    patient_first_name: str = ""
    member_id: str = ""

    @property
    def accepted(self) -> bool:
        """Did the claim enter adjudication?"""
        return self.category_code.strip().upper() in ACCEPTED_CATEGORIES

    @property
    def rejected(self) -> bool:
        """Was the claim returned without being adjudicated?

        **Not a denial.** A denial is a payer refusing to pay a claim it
        adjudicated, and arrives on an 835. This is the claim never getting
        that far (PD-4).
        """
        return self.category_code.strip().upper() in REJECTED_CATEGORIES

    @property
    def is_known_category(self) -> bool:
        """Is the category one this parser has an opinion about?

        A category outside both sets is neither accepted nor rejected here,
        and the loader treats that as a reason to stop rather than to guess
        (PD-18). Inventing a verdict for an unrecognised code is how a claim
        silently acquires an outcome nobody chose.
        """
        return self.accepted or self.rejected


@dataclass(frozen=True, slots=True)
class ClaimStatusResponse:
    """One 277 or 277CA transaction set."""

    #: ST02.
    transaction_control_number: str
    #: BHT03/04, the sender's reference and the date it was produced.
    reference: str = ""
    created_on: date | None = None
    payer_name: str = ""
    payer_identifier: str = ""
    statuses: tuple[ClaimStatus, ...] = ()

    @property
    def accepted_claims(self) -> tuple[ClaimStatus, ...]:
        return tuple(s for s in self.statuses if s.accepted)

    @property
    def rejected_claims(self) -> tuple[ClaimStatus, ...]:
        return tuple(s for s in self.statuses if s.rejected)

    @property
    def unknown_categories(self) -> tuple[str, ...]:
        """Category codes this parser does not classify.

        Reported so a loader can refuse the file rather than silently
        treating an unrecognised verdict as one of the two it knows.
        """
        return tuple(sorted({s.category_code for s in self.statuses if not s.is_known_category}))


@dataclass
class _StatusBuilder:
    """Mutable accumulator for one 2200 loop."""

    claim_id: str = ""
    payer_claim_number: str = ""
    patient_last_name: str = ""
    patient_first_name: str = ""
    member_id: str = ""
    entries: list[dict[str, object]] = field(default_factory=list)

    def build(self) -> list[ClaimStatus]:
        return [
            ClaimStatus(
                claim_id=self.claim_id,
                payer_claim_number=self.payer_claim_number,
                patient_last_name=self.patient_last_name,
                patient_first_name=self.patient_first_name,
                member_id=self.member_id,
                **entry,  # type: ignore[arg-type]
            )
            for entry in self.entries
        ]


def parse_277(transaction: TransactionSet, separators: Separators) -> ClaimStatusResponse:
    """Read one 277 or 277CA transaction set.

    The same reader serves both. A 277CA is a 277 whose statuses are the
    payer's front-end verdict on a submission; a 277 proper answers a status
    enquiry. The segments are the same and the difference is in what the
    caller does with the result.
    """
    reference = ""
    created_on: date | None = None
    payer_name = ""
    payer_identifier = ""

    # Patient demographics arrive above the claim loop and apply to every
    # claim below them, so they are carried down rather than re-read.
    patient_last = patient_first = member_id = ""

    statuses: list[_StatusBuilder] = []
    current: _StatusBuilder | None = None

    for segment in transaction.segments:
        tag = segment.tag

        if tag == "BHT":
            reference = segment.get(3).strip()
            created_on = optional_date(segment.get(4), field_name="BHT04", segment=segment)

        elif tag == "HL":
            level = segment.get(3).strip()
            if level in {_HL_SUBSCRIBER, _HL_DEPENDENT}:
                patient_last = patient_first = member_id = ""
            current = None

        elif tag == "NM1":
            entity = segment.get(1).strip()
            if entity == "PR":
                payer_name = segment.get(3).strip()
                payer_identifier = segment.get(9).strip()
            elif entity in {"QC", "IL"}:
                patient_last = segment.get(3).strip()
                patient_first = segment.get(4).strip()
                member_id = segment.get(9).strip()

        elif tag == "TRN":
            # TRN02 in the claim loop is the submitter's claim control number.
            current = _StatusBuilder(
                claim_id=segment.get(2).strip(),
                patient_last_name=patient_last,
                patient_first_name=patient_first,
                member_id=member_id,
            )
            statuses.append(current)

        elif tag == "STC":
            if current is None:
                raise MalformedX12Error(
                    reason="STC status outside any claim loop, so it names no claim",
                    field="STC",
                    segment=str(segment),
                )
            composite = segment.composite(1, separators)
            if not composite:
                raise MalformedX12Error(
                    reason="STC01 carries no status category",
                    field="STC01",
                    segment=str(segment),
                )
            current.entries.append(
                {
                    "category_code": composite[0].strip(),
                    "status_code": composite[1].strip() if len(composite) > 1 else "",
                    "entity_code": (composite[2].strip() if len(composite) > 2 else ""),
                    "effective_date": optional_date(
                        segment.get(2), field_name="STC02", segment=segment
                    ),
                    "action_code": segment.get(3).strip(),
                    "total_charge": optional_amount(
                        segment.get(4), field_name="STC04", segment=segment
                    )
                    if segment.get(4).strip()
                    else None,
                    "description": segment.get(12).strip(),
                }
            )

        elif tag == "REF" and current is not None and segment.get(1).strip() == "1K":
            current.payer_claim_number = segment.get(2).strip()

    return ClaimStatusResponse(
        transaction_control_number=transaction.control_number,
        reference=reference,
        created_on=created_on,
        payer_name=payer_name,
        payer_identifier=payer_identifier,
        statuses=tuple(status for builder in statuses for status in builder.build()),
    )
