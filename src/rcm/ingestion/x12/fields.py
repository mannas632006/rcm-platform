"""Element parsers shared by the transaction parsers (M1.5).

These started inside the 835 parser, where they were written. The 837 parser
needs exactly the same reading of money, dates and integers, and the one thing
that must not happen is a second implementation that rounds or defaults
differently -- a claim whose charge is read one way and whose payment is read
another produces a reconciliation failure that looks like a payer problem.

The rule every function here follows: **absent is not malformed**. X12 permits
trailing elements to be dropped, so an element that is not there has a defined
meaning and may have a default. An element that is there and unreadable has no
defined meaning, so it raises. Substituting a zero or today's date for
something a file actually said is how a wrong number reaches a ledger with
nothing in the logs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from rcm.ingestion.x12.tokenizer import Segment
from rcm.platform.errors import MalformedX12Error

ZERO: Final = Decimal("0.00")

#: X12 dates in these transactions are CCYYMMDD.
_DATE_WIDTH: Final = 8


def parse_amount(raw: str, *, field_name: str, segment: Segment) -> Decimal:
    """Parse X12 money into `Decimal`, or raise.

    There is deliberately no default. An amount this function cannot read is a
    file we do not understand, and substituting zero would put a wrong number
    into a ledger silently -- which CLAUDE.md section 0 calls a compliance
    event rather than a bug.
    """
    text = raw.strip()
    if not text:
        raise MalformedX12Error(
            reason="monetary element is empty", field=field_name, segment=str(segment)
        )
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise MalformedX12Error(
            reason="monetary element is not a number",
            field=field_name,
            segment=str(segment),
        ) from exc


def optional_amount(raw: str, *, field_name: str, segment: Segment) -> Decimal:
    """An amount X12 allows to be omitted. Absent means zero; malformed does not."""
    if not raw.strip():
        return ZERO
    return parse_amount(raw, field_name=field_name, segment=segment)


def parse_date(raw: str, *, field_name: str, segment: Segment) -> date:
    """Parse a CCYYMMDD date element."""
    text = raw.strip()
    # Built as a calendar date rather than parsed into a datetime and
    # truncated: a date of service is a day, not an instant, and giving it a
    # notional midnight invites a timezone conversion later that moves it.
    if len(text) != _DATE_WIDTH or not text.isdigit():
        raise MalformedX12Error(
            reason="date element is not 8 digits of CCYYMMDD",
            field=field_name,
            segment=str(segment),
        )
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as exc:
        raise MalformedX12Error(
            reason="date element is not a real calendar date",
            field=field_name,
            segment=str(segment),
        ) from exc


def optional_date(raw: str, *, field_name: str, segment: Segment) -> date | None:
    """A date X12 allows to be omitted. Absent means `None`; malformed raises."""
    if not raw.strip():
        return None
    return parse_date(raw, field_name=field_name, segment=segment)


def parse_quantity(raw: str, *, field_name: str, segment: Segment) -> Decimal:
    """Parse a quantity element.

    `Decimal` rather than `int` because SV104 is a quantity, and anaesthesia
    minutes and drug units are both legitimately fractional. Reading it as an
    integer truncates 1.5 units of a drug to 1 and bills the wrong amount.
    """
    text = raw.strip()
    if not text:
        raise MalformedX12Error(
            reason="quantity element is empty", field=field_name, segment=str(segment)
        )
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise MalformedX12Error(
            reason="quantity element is not a number",
            field=field_name,
            segment=str(segment),
        ) from exc
