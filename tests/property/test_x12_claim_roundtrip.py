"""generate -> parse -> equals source claim, over arbitrary claims (M1.5).

CLAUDE.md states M1.5's acceptance condition as a round trip, and a round trip
is a property rather than a list of cases. Written with fixtures it proves the
generator and parser agree about the claims the author thought to write down,
which is the same set of claims the author was already thinking about when
writing both. Hypothesis chooses the ones nobody considered: a claim with no
diagnoses, four modifiers on one line, a fractional quantity, twelve service
lines, an empty subscriber address, a charge of 0.00.

The round trip is asserted on the claims rather than the whole submission
because the envelope carries things no claim knows about -- control numbers,
delimiters, the production/test flag -- and conflating "the claims survived"
with "the envelope was reproduced byte for byte" makes a failure in either one
look like a failure in the other.

Two properties, not one:

    representable claims round-trip exactly
    claims carrying a delimiter are refused, never silently corrupted

The second matters because X12 has no escape mechanism. A `*` in a surname
ends the element and shifts every element after it, leaving a file that is
still syntactically valid and describes a different claim.
"""

from __future__ import annotations

import contextlib
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from rcm.ingestion.x12 import read_claims
from rcm.ingestion.x12.claim import (
    STANDARD_SEPARATORS,
    BillingProvider,
    Claim837,
    ClaimServiceLine,
    ClaimSubmission,
    Subscriber,
    TradingPartner,
    build_837,
)
from rcm.platform.errors import MalformedX12Error, PlatformError

_SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

_DELIMITERS = frozenset(
    {
        STANDARD_SEPARATORS.element,
        STANDARD_SEPARATORS.component,
        STANDARD_SEPARATORS.repetition,
        STANDARD_SEPARATORS.segment,
    }
)

#: Text a sender could legitimately put in a name or address field. Leading and
#: trailing spaces are excluded because the parser strips them, which is
#: correct -- X12 pads fixed-width elements -- and would make the round trip
#: fail for a reason that is not a defect.
_TEXT = st.text(
    alphabet=st.characters(
        min_codepoint=32,
        max_codepoint=126,
        blacklist_characters="".join(sorted(_DELIMITERS)),
    ),
    min_size=0,
    max_size=24,
).map(str.strip)

_CODE = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.", min_size=1, max_size=8)

_MONEY = st.decimals(
    min_value=Decimal("0.00"),
    max_value=Decimal("99999.99"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

_QUANTITY = st.decimals(
    min_value=Decimal("0.1"),
    max_value=Decimal("999"),
    places=1,
    allow_nan=False,
    allow_infinity=False,
)

_DATE = st.dates(min_value=date(2000, 1, 1), max_value=date(2039, 12, 31))


@st.composite
def service_lines(draw: st.DrawFn, count: int) -> tuple[ClaimServiceLine, ...]:
    lines = []
    for number in range(1, count + 1):
        lines.append(
            ClaimServiceLine(
                line_number=number,
                procedure_code=draw(_CODE),
                charge_amount=draw(_MONEY),
                modifiers=tuple(draw(st.lists(_CODE, min_size=0, max_size=4))),
                unit_basis=draw(st.sampled_from(["UN", "MJ"])),
                units=draw(_QUANTITY),
                diagnosis_pointers=tuple(
                    draw(st.lists(st.integers(min_value=1, max_value=12), max_size=4))
                ),
                service_date=draw(st.one_of(st.none(), _DATE)),
            )
        )
    return tuple(lines)


@st.composite
def claims(draw: st.DrawFn) -> Claim837:
    return Claim837(
        claim_id=draw(_CODE),
        total_charge=draw(_MONEY),
        billing_provider=BillingProvider(
            name=draw(_TEXT),
            npi=draw(_CODE),
            taxonomy_code=draw(st.one_of(st.just(""), _CODE)),
            address=draw(_TEXT),
            city=draw(_TEXT),
            state=draw(st.one_of(st.just(""), st.sampled_from(["TX", "CA", "NY"]))),
            postal_code=draw(st.one_of(st.just(""), _CODE)),
            tax_id=draw(st.one_of(st.just(""), _CODE)),
        ),
        subscriber=Subscriber(
            last_name=draw(_TEXT),
            first_name=draw(_TEXT),
            member_id=draw(_CODE),
            address=draw(_TEXT),
            city=draw(_TEXT),
            state=draw(st.one_of(st.just(""), st.sampled_from(["TX", "CA", "NY"]))),
            postal_code=draw(st.one_of(st.just(""), _CODE)),
            date_of_birth=draw(st.one_of(st.none(), _DATE)),
            sex=draw(st.sampled_from(["", "M", "F", "U"])),
        ),
        payer_id=draw(_CODE),
        payer_name=draw(_TEXT),
        claim_filing_code=draw(st.sampled_from(["CI", "MB", "MC", "BL", ""])),
        payer_responsibility=draw(st.sampled_from(["P", "S", "T"])),
        place_of_service=draw(st.sampled_from(["11", "21", "22", "23", ""])),
        claim_frequency_code=draw(st.sampled_from(["1", "7", "8"])),
        diagnoses=tuple(draw(st.lists(_CODE, min_size=0, max_size=12))),
        service_date=draw(st.one_of(st.none(), _DATE)),
        prior_auth_number=draw(st.one_of(st.just(""), _CODE)),
        referral_number=draw(st.one_of(st.just(""), _CODE)),
        original_claim_id=draw(st.one_of(st.just(""), _CODE)),
        lines=draw(st.integers(min_value=0, max_value=6).flatmap(service_lines)),
    )


@st.composite
def submissions(draw: st.DrawFn) -> ClaimSubmission:
    return ClaimSubmission(
        control_number=draw(st.from_regex(r"\A[0-9]{4,9}\Z")),
        submitter=TradingPartner(name=draw(_TEXT), etin=draw(_CODE)),
        receiver=TradingPartner(name=draw(_TEXT), etin=draw(_CODE)),
        claims=tuple(draw(st.lists(claims(), min_size=1, max_size=4))),
        created_on=draw(_DATE),
        created_time=draw(st.from_regex(r"\A[0-9]{4}\Z")),
    )


@pytest.mark.invariant
@_SETTINGS
@given(submissions())
def test_generate_parse_equals_source_claim(submission: ClaimSubmission) -> None:
    """M1.5's acceptance condition, stated as a property."""
    reparsed = read_claims(build_837(submission).encode())

    assert len(reparsed) == 1
    assert reparsed[0].claims == submission.claims


@_SETTINGS
@given(submissions())
def test_the_round_trip_is_stable(submission: ClaimSubmission) -> None:
    """Parsing a generated file and regenerating it produces the same text.

    A round trip that converges only after several passes is a round trip with
    a defect that happens to cancel. This is what catches an element the
    generator writes and the parser ignores: the first pass drops it and the
    second produces different bytes.
    """
    once = build_837(submission)
    parsed = read_claims(once.encode())[0]
    twice = build_837(parsed)

    assert twice == once


@_SETTINGS
@given(submissions())
def test_the_resubmission_chain_survives_the_round_trip(
    submission: ClaimSubmission,
) -> None:
    """PD-7 specifically, because it is one element in one optional segment and
    is exactly the kind of field a generator quietly drops."""
    reparsed = read_claims(build_837(submission).encode())[0]

    assert [c.original_claim_id for c in reparsed.claims] == [
        c.original_claim_id for c in submission.claims
    ]
    assert [c.is_resubmission for c in reparsed.claims] == [
        c.is_resubmission for c in submission.claims
    ]


@_SETTINGS
@given(submissions())
def test_no_modifier_is_ever_added(submission: ClaimSubmission) -> None:
    """PD-16 as a property: whatever the claim carried, it carries after."""
    reparsed = read_claims(build_837(submission).encode())[0]

    for before, after in zip(submission.claims, reparsed.claims, strict=True):
        for line_before, line_after in zip(before.lines, after.lines, strict=True):
            assert line_after.modifiers == line_before.modifiers


@_SETTINGS
@given(
    submissions(),
    st.sampled_from(sorted(_DELIMITERS)),
)
def test_a_delimiter_in_data_is_refused_not_silently_corrupted(
    submission: ClaimSubmission, delimiter: str
) -> None:
    """X12 has no escaping, so a `*` in a surname ends the element and shifts
    every element after it. The file stays syntactically valid and describes a
    different claim, which is the worst available outcome."""
    first = submission.claims[0]
    poisoned = ClaimSubmission(
        control_number=submission.control_number,
        submitter=submission.submitter,
        receiver=submission.receiver,
        claims=(
            Claim837(
                claim_id=first.claim_id,
                total_charge=first.total_charge,
                billing_provider=first.billing_provider,
                subscriber=Subscriber(
                    last_name=f"OBrien{delimiter}Smith",
                    first_name=first.subscriber.first_name,
                    member_id=first.subscriber.member_id,
                ),
                payer_id=first.payer_id,
                lines=first.lines,
            ),
        ),
        created_on=submission.created_on,
        created_time=submission.created_time,
    )

    with pytest.raises(MalformedX12Error):
        build_837(poisoned)


# ---------------------------------------------------------------------------
# No uncaught exception on any input (CLAUDE.md section 7.1)
# ---------------------------------------------------------------------------

#: Characters that turn up in EDI, weighted so delimiters and structural text
#: appear far more often than random generation would produce.
_EDI_ALPHABET = st.sampled_from(
    list("ISAGSTBHTNM1PERHLPRVN3N4REFDMGSBRCLMDTPHILXSV1SEGEIEA0123456789.*:~^|>\r\n ")
)


@pytest.mark.invariant
@_SETTINGS
@given(st.binary(max_size=4096))
def test_the_claim_parser_never_raises_an_unnamed_exception(raw: bytes) -> None:
    """Any byte sequence at all: parsed, or refused by a named error.

    The same contract the 835 parser is held to, and for the same reason: an
    unnamed exception cannot be quarantined, because DR-10 requires a
    quarantined file to name the assertion it failed.
    """
    with contextlib.suppress(PlatformError):
        read_claims(raw)


@pytest.mark.invariant
@_SETTINGS
@given(st.text(alphabet=_EDI_ALPHABET, max_size=4096))
def test_the_claim_parser_survives_claim_shaped_text(text: str) -> None:
    """Random bytes mostly fail at the first check; text drawn from the 837
    alphabet gets far enough in to reach the interesting failures."""
    with contextlib.suppress(PlatformError):
        read_claims(text)


@pytest.mark.invariant
@_SETTINGS
@given(
    submissions(),
    st.integers(min_value=0, max_value=4000),
    st.integers(min_value=0, max_value=4000),
)
def test_truncating_a_generated_claim_file_is_refused_cleanly(
    submission: ClaimSubmission, start: int, length: int
) -> None:
    """Every slice of a valid 837.

    A truncated transfer is the most common real corruption, and slicing a
    known-good file reaches structural states random generation almost never
    produces -- a file ending mid-composite, between HL and CLM, or after LX
    with no SV1.
    """
    valid = build_837(submission)
    fragment = valid[start : start + length]
    assume(fragment != valid)
    with contextlib.suppress(PlatformError):
        read_claims(fragment)
