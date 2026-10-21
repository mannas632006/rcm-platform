"""X12 interchange envelope: ISA/GS/ST and their trailers.

The envelope is three nested wrappers, each with a header, a trailer, a control
number that must match between them, and a count the trailer asserts:

    ISA ... IEA     interchange   IEA01 = group count,       IEA02 = ISA13
      GS ... GE     functional    GE01  = transaction count, GE02  = GS06
        ST ... SE   transaction   SE01  = segment count,     SE02  = ST02

Those checks are the file's own integrity statement, and they are cheap. A
truncated transfer, a file concatenated with another, or a sender that dropped
a segment all show up here rather than as a mysteriously short claim list three
stages later. DR-10 requires a quarantine to name the assertion that failed,
which is why each mismatch raises with the expected and actual values attached
rather than a single "malformed file".

The counts are checked but the *parse* does not depend on them: segments are
assigned to a transaction set by position between ST and SE. A sender whose
SE01 is wrong by one has a file we can still read and should still refuse,
and those are different decisions -- `parse_interchange` raises, while
`iter_transaction_sets` is available for the caller that has decided to read a
file anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from rcm.ingestion.x12.tokenizer import Segment, Separators
from rcm.platform.errors import MalformedX12Error

#: Segment tags that open and close each envelope level.
_INTERCHANGE_HEADER: Final = "ISA"
_INTERCHANGE_TRAILER: Final = "IEA"
_GROUP_HEADER: Final = "GS"
_GROUP_TRAILER: Final = "GE"
_TRANSACTION_HEADER: Final = "ST"
_TRANSACTION_TRAILER: Final = "SE"


def _as_int(value: str, *, field: str, segment: Segment) -> int:
    """Parse a control count, naming the field if it is not a number."""
    try:
        return int(value.strip() or "0")
    except ValueError as exc:
        raise MalformedX12Error(
            reason="control value is not numeric",
            field=field,
            segment=str(segment),
        ) from exc


def _control_equal(left: str, right: str) -> bool:
    """Compare control numbers the way senders actually write them.

    ISA13 is zero-padded to nine characters and IEA02 usually echoes it
    verbatim, but not always -- some senders pad one and not the other, and the
    standard's own examples are inconsistent. Comparing the numeric value is
    what the check is actually for; comparing the literal strings rejects files
    that are correct.
    """
    stripped_left, stripped_right = left.strip(), right.strip()
    if stripped_left.isdigit() and stripped_right.isdigit():
        return int(stripped_left) == int(stripped_right)
    return stripped_left == stripped_right


@dataclass(frozen=True, slots=True)
class TransactionSet:
    """One ST...SE transaction and the segments between them."""

    #: ST01, the transaction set identifier: "835" for remittance, "837" for a
    #: claim. Callers dispatch on this.
    identifier: str
    #: ST02, the sender's control number for this transaction.
    control_number: str
    #: ST03 where present: the implementation guide version, e.g. 005010X221A1.
    implementation_reference: str
    #: Every segment strictly between ST and SE. The ST and SE themselves are
    #: envelope, not content, and no transaction parser should have to skip
    #: them.
    segments: tuple[Segment, ...]
    #: The declared and actual segment counts, kept so a report can quote both.
    declared_segment_count: int
    actual_segment_count: int


@dataclass(frozen=True, slots=True)
class FunctionalGroup:
    """One GS...GE group: transactions of a single type from a single sender."""

    #: GS01: "HP" for remittance advice, "HC" for health care claim.
    functional_identifier: str
    sender_code: str
    receiver_code: str
    #: GS08: the version/release/industry identifier.
    version: str
    control_number: str
    transaction_sets: tuple[TransactionSet, ...]


@dataclass(frozen=True, slots=True)
class Interchange:
    """One ISA...IEA interchange: the whole file."""

    sender_qualifier: str
    sender_id: str
    receiver_qualifier: str
    receiver_id: str
    #: ISA13.
    control_number: str
    #: ISA15: "P" for production, "T" for test. A production pipeline that
    #: ingests a test interchange has posted money against a file the sender
    #: never meant to be real, so this is kept and checked upstream.
    usage_indicator: str
    separators: Separators
    functional_groups: tuple[FunctionalGroup, ...]

    @property
    def is_production(self) -> bool:
        return self.usage_indicator.strip().upper() == "P"

    def transaction_sets(self, identifier: str | None = None) -> tuple[TransactionSet, ...]:
        """Every transaction in the interchange, optionally of one type."""
        found = tuple(
            transaction
            for group in self.functional_groups
            for transaction in group.transaction_sets
        )
        if identifier is None:
            return found
        return tuple(t for t in found if t.identifier == identifier)


def _require(condition: bool, **context: object) -> None:
    if not condition:
        raise MalformedX12Error(**context)


def _parse_transaction_sets(
    segments: tuple[Segment, ...], group_control: str
) -> tuple[TransactionSet, ...]:
    """Read ST...SE pairs out of the segments inside one functional group."""
    transactions: list[TransactionSet] = []
    opening: Segment | None = None
    body: list[Segment] = []

    for segment in segments:
        if segment.tag == _TRANSACTION_HEADER:
            _require(
                opening is None,
                reason="ST without a closing SE",
                segment=str(segment),
                group_control=group_control,
            )
            opening = segment
            body = []
            continue

        if segment.tag == _TRANSACTION_TRAILER:
            _require(
                opening is not None,
                reason="SE without a preceding ST",
                segment=str(segment),
                group_control=group_control,
            )
            assert opening is not None  # narrowed by _require; mypy cannot see it
            declared = _as_int(segment.get(1), field="SE01", segment=segment)
            # SE01 counts ST and SE themselves, so the body is two fewer.
            actual = len(body) + 2
            _require(
                declared == actual,
                reason="SE01 segment count does not match the segments present",
                assertion="SE01 == count(ST..SE)",
                declared=declared,
                actual=actual,
                transaction_control=opening.get(2),
            )
            _require(
                _control_equal(opening.get(2), segment.get(2)),
                reason="SE02 does not match ST02",
                assertion="SE02 == ST02",
                st02=opening.get(2),
                se02=segment.get(2),
            )
            transactions.append(
                TransactionSet(
                    identifier=opening.get(1),
                    control_number=opening.get(2),
                    implementation_reference=opening.get(3),
                    segments=tuple(body),
                    declared_segment_count=declared,
                    actual_segment_count=actual,
                )
            )
            opening = None
            body = []
            continue

        if opening is not None:
            body.append(segment)

    _require(
        opening is None,
        reason="interchange ends inside a transaction set",
        assertion="every ST has an SE",
        transaction_control=opening.get(2) if opening else "",
    )
    return tuple(transactions)


def parse_interchange(segments: tuple[Segment, ...], separators: Separators) -> Interchange:
    """Validate the envelope and return its structure.

    Every control-number and count assertion in the file is checked here. A
    failure names the assertion, because DR-10 requires a quarantined file to
    say which gate it failed rather than that it failed one.
    """
    _require(
        bool(segments) and segments[0].tag == _INTERCHANGE_HEADER,
        reason="interchange does not start with ISA",
        found=segments[0].tag if segments else "",
    )
    isa = segments[0]

    trailers = [s for s in segments if s.tag == _INTERCHANGE_TRAILER]
    _require(
        len(trailers) == 1,
        reason="interchange must have exactly one IEA",
        assertion="count(IEA) == 1",
        found=len(trailers),
    )
    iea = trailers[0]
    _require(
        _control_equal(isa.get(13), iea.get(2)),
        reason="IEA02 does not match ISA13",
        assertion="IEA02 == ISA13",
        isa13=isa.get(13),
        iea02=iea.get(2),
    )

    groups: list[FunctionalGroup] = []
    opening: Segment | None = None
    body: list[Segment] = []

    for segment in segments[1:]:
        if segment.tag == _GROUP_HEADER:
            _require(
                opening is None,
                reason="GS without a closing GE",
                segment=str(segment),
            )
            opening = segment
            body = []
            continue

        if segment.tag == _GROUP_TRAILER:
            _require(
                opening is not None,
                reason="GE without a preceding GS",
                segment=str(segment),
            )
            assert opening is not None  # narrowed by _require
            _require(
                _control_equal(opening.get(6), segment.get(2)),
                reason="GE02 does not match GS06",
                assertion="GE02 == GS06",
                gs06=opening.get(6),
                ge02=segment.get(2),
            )
            transactions = _parse_transaction_sets(tuple(body), opening.get(6))
            declared = _as_int(segment.get(1), field="GE01", segment=segment)
            _require(
                declared == len(transactions),
                reason="GE01 transaction count does not match the transactions present",
                assertion="GE01 == count(ST)",
                declared=declared,
                actual=len(transactions),
                group_control=opening.get(6),
            )
            groups.append(
                FunctionalGroup(
                    functional_identifier=opening.get(1),
                    sender_code=opening.get(2),
                    receiver_code=opening.get(3),
                    version=opening.get(8),
                    control_number=opening.get(6),
                    transaction_sets=transactions,
                )
            )
            opening = None
            body = []
            continue

        if segment.tag == _INTERCHANGE_TRAILER:
            continue
        if opening is not None:
            body.append(segment)

    _require(
        opening is None,
        reason="interchange ends inside a functional group",
        assertion="every GS has a GE",
    )

    declared_groups = _as_int(iea.get(1), field="IEA01", segment=iea)
    _require(
        declared_groups == len(groups),
        reason="IEA01 group count does not match the groups present",
        assertion="IEA01 == count(GS)",
        declared=declared_groups,
        actual=len(groups),
    )

    return Interchange(
        sender_qualifier=isa.get(5),
        sender_id=isa.get(6).strip(),
        receiver_qualifier=isa.get(7),
        receiver_id=isa.get(8).strip(),
        control_number=isa.get(13),
        usage_indicator=isa.get(15),
        separators=separators,
        functional_groups=tuple(groups),
    )
