"""999 functional acknowledgement: did the file parse at all (M1.6).

A 999 answers one question, and it is a narrow one: was the transmission
syntactically valid X12? It says nothing about whether a payer will accept the
claims inside it, and DOC-02 is explicit that treating it as more is a
compliance problem rather than an optimism problem:

    BR-M8-02 (MUST): A claim MUST NOT be considered submitted until a 277CA
    acceptance is received. 999 acceptance alone is insufficient -- it
    confirms syntax, not acceptance.

So this module reads the answer and nothing further. A 999 rejection is a
**rejection** in DOC-02 section 2.5's sense: the claim never reached
adjudication, it is not appealable, the timely-filing clock keeps running, and
it must never be counted as a denial (PD-4, BR-GLOBAL-02).

The error detail matters more here than in most parsers. A 999 that says only
"rejected" sends an operator to open the file by hand; AK2/IK3/IK4 name the
transaction, the segment, its position and the element that failed, and that
is the difference between a queue someone can work and a queue they cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from rcm.ingestion.x12.envelope import TransactionSet
from rcm.ingestion.x12.tokenizer import Segment, Separators
from rcm.platform.errors import MalformedX12Error

#: AK901 / IK501 acknowledgement codes (X12 code list 715).
#:
#:   A  accepted
#:   E  accepted, but errors were noted
#:   M  rejected, message authentication code failed
#:   P  partially accepted -- at least one transaction set was rejected
#:   R  rejected
#:   W  rejected, assurance failed validity tests
#:   X  rejected, content could not be analyzed after decryption
ACCEPTED_CODES: Final[frozenset[str]] = frozenset({"A", "E"})

#: `P` is deliberately on this side. "Partially accepted" means something in
#: the transmission was rejected, and a submission pipeline that treats it as
#: acceptance loses exactly the claims that failed (PD-18: fail closed).
REJECTED_CODES: Final[frozenset[str]] = frozenset({"M", "P", "R", "W", "X"})


@dataclass(frozen=True, slots=True)
class SegmentError:
    """IK3/IK4: which segment failed, and which element in it."""

    #: IK301, the segment tag that failed, e.g. "CLM".
    segment_id: str
    #: IK302, its position in the transaction set. An operator opening the
    #: file needs this to find the segment; the tag alone appears many times.
    segment_position: int | None
    #: IK303, the loop the segment sat in.
    loop_identifier: str = ""
    #: IK304, why the segment was refused.
    error_code: str = ""
    #: IK401, the element within the segment.
    element_position: int | None = None
    #: IK403, why the element was refused.
    element_error_code: str = ""
    #: IK404, the value that failed. Kept because "invalid date" without the
    #: date is not actionable -- and it is the sender's own data coming back,
    #: so nothing here is PHI the platform did not already hold.
    bad_value: str = ""


@dataclass(frozen=True, slots=True)
class TransactionAcknowledgement:
    """AK2/IK5: the verdict on one transaction set inside the group."""

    #: AK201, the transaction set identifier, e.g. "837".
    transaction_set_identifier: str
    #: AK202, the sender's control number for it -- the handle that ties this
    #: verdict back to the file that was sent.
    control_number: str
    #: IK501 acknowledgement code.
    status: str = ""
    errors: tuple[SegmentError, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status.strip().upper() in ACCEPTED_CODES

    @property
    def rejected(self) -> bool:
        return self.status.strip().upper() in REJECTED_CODES


@dataclass(frozen=True, slots=True)
class FunctionalAcknowledgement:
    """One 999: the verdict on one functional group that was sent."""

    #: AK101, the functional identifier of the group being acknowledged.
    functional_identifier: str
    #: AK102, that group's control number.
    group_control_number: str
    #: AK103, the implementation guide the group claimed.
    version: str
    #: AK901 acknowledgement code for the group as a whole.
    status: str
    #: AK902/03/04: included, received, accepted.
    transactions_included: int = 0
    transactions_received: int = 0
    transactions_accepted: int = 0
    transactions: tuple[TransactionAcknowledgement, ...] = ()

    @property
    def accepted(self) -> bool:
        """Syntactically valid. **Not** the same as accepted by a payer.

        BR-M8-02: a claim is not submitted until a 277CA says so, and reading
        this property as submission is the mistake the rule exists to prevent.
        """
        return self.status.strip().upper() in ACCEPTED_CODES

    @property
    def rejected(self) -> bool:
        return self.status.strip().upper() in REJECTED_CODES

    @property
    def rejected_transactions(self) -> tuple[TransactionAcknowledgement, ...]:
        return tuple(t for t in self.transactions if t.rejected)

    @property
    def counts_reconcile(self) -> bool:
        """Does AK902 agree with AK903, and is AK904 no larger?

        Reported rather than enforced, matching how the 835 parser reports
        `balances`: a 999 whose own counts disagree is worth an operator's
        attention, and deciding what to do about it is the loader's call.
        """
        if self.transactions_included != self.transactions_received:
            return False
        return self.transactions_accepted <= self.transactions_received


@dataclass
class _TransactionBuilder:
    """Mutable accumulator for one AK2 loop."""

    transaction_set_identifier: str
    control_number: str
    status: str = ""
    errors: list[SegmentError] = field(default_factory=list)
    #: IK3 opens a segment-error group that following IK4s belong to.
    pending_segment: SegmentError | None = None
    #: Whether any IK4 has refined the open IK3. An IK3 with no IK4 is a
    #: finding in its own right and is kept; once an IK4 has named an
    #: element, the bare segment row would duplicate it. The group stays
    #: open either way, because several IK4s may follow one IK3 and closing
    #: on the first drops the rest.
    pending_refined: bool = False

    def close_segment(self) -> None:
        if self.pending_segment is not None and not self.pending_refined:
            self.errors.append(self.pending_segment)
        self.pending_segment = None
        self.pending_refined = False

    def build(self) -> TransactionAcknowledgement:
        self.close_segment()
        return TransactionAcknowledgement(
            transaction_set_identifier=self.transaction_set_identifier,
            control_number=self.control_number,
            status=self.status,
            errors=tuple(self.errors),
        )


def _optional_position(raw: str, *, field_name: str, segment: Segment) -> int | None:
    """A 1-based position element, which senders sometimes omit."""
    text = raw.strip()
    if not text:
        return None
    if not text.isdigit():
        raise MalformedX12Error(
            reason="position element is not a number", field=field_name, segment=str(segment)
        )
    return int(text)


def _count(raw: str, *, field_name: str, segment: Segment) -> int:
    text = raw.strip()
    if not text:
        return 0
    if not text.lstrip("-").isdigit():
        raise MalformedX12Error(
            reason="count element is not a number", field=field_name, segment=str(segment)
        )
    return int(text)


def parse_999(transaction: TransactionSet, separators: Separators) -> FunctionalAcknowledgement:
    """Read one 999 transaction set.

    Raises `MalformedX12Error` for a file that is not a valid 999. A 999 that
    says a submission was *rejected* is not malformed -- it is a valid file
    delivering bad news, and conflating the two loses the news.
    """
    del separators  # 999 carries no composites this parser reads

    functional_identifier = ""
    group_control_number = ""
    version = ""
    status = ""
    included = received = accepted = 0

    transactions: list[_TransactionBuilder] = []
    current: _TransactionBuilder | None = None

    for segment in transaction.segments:
        tag = segment.tag

        if tag == "AK1":
            functional_identifier = segment.get(1).strip()
            group_control_number = segment.get(2).strip()
            version = segment.get(3).strip()

        elif tag == "AK2":
            current = _TransactionBuilder(
                transaction_set_identifier=segment.get(1).strip(),
                control_number=segment.get(2).strip(),
            )
            transactions.append(current)

        elif tag == "IK3" and current is not None:
            current.close_segment()
            current.pending_segment = SegmentError(
                segment_id=segment.get(1).strip(),
                segment_position=_optional_position(
                    segment.get(2), field_name="IK302", segment=segment
                ),
                loop_identifier=segment.get(3).strip(),
                error_code=segment.get(4).strip(),
            )

        elif tag == "IK4" and current is not None and current.pending_segment is not None:
            # IK4 refines the IK3 that opened the group. Several IK4s may
            # follow one IK3, each naming a different element, so each becomes
            # its own row rather than overwriting the last.
            opened = current.pending_segment
            current.errors.append(
                SegmentError(
                    segment_id=opened.segment_id,
                    segment_position=opened.segment_position,
                    loop_identifier=opened.loop_identifier,
                    error_code=opened.error_code,
                    element_position=_optional_position(
                        segment.get(1), field_name="IK401", segment=segment
                    ),
                    element_error_code=segment.get(3).strip(),
                    bad_value=segment.get(4).strip(),
                )
            )
            current.pending_refined = True

        elif tag == "IK5" and current is not None:
            current.close_segment()
            current.status = segment.get(1).strip()

        elif tag == "AK9":
            if current is not None:
                current.close_segment()
            status = segment.get(1).strip()
            included = _count(segment.get(2), field_name="AK902", segment=segment)
            received = _count(segment.get(3), field_name="AK903", segment=segment)
            accepted = _count(segment.get(4), field_name="AK904", segment=segment)

    if not status:
        raise MalformedX12Error(
            reason="999 carries no AK9 trailer, so it states no verdict",
            field="AK9",
            segment=f"ST[{transaction.control_number}]",
        )

    return FunctionalAcknowledgement(
        functional_identifier=functional_identifier,
        group_control_number=group_control_number,
        version=version,
        status=status,
        transactions_included=included,
        transactions_received=received,
        transactions_accepted=accepted,
        transactions=tuple(t.build() for t in transactions),
    )
