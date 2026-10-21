"""X12 parsing survives any input at all (M1.1, SR-12).

CLAUDE.md section 7.1 states the acceptance condition for this suite in one
line: *"Parsers: no uncaught exception on any input."*

That is a stronger claim than "handles the malformed files we thought of", and
it is the one that matters, because the input arrives from outside the trust
boundary. A `ValueError` escaping the parser cannot be quarantined -- DR-10
requires a quarantined file to name the assertion it failed, and an unnamed
exception names nothing. So the property asserted here is not that bad input is
rejected, but that it is rejected *by a `PlatformError`*, every time, whatever
the bytes were.

Hypothesis is used rather than a list of hand-written bad files because the
interesting failures are the ones nobody thought to write down: a file that is
only an ISA header, delimiters that collide, a segment terminator inside what
looks like data, a CAS segment truncated mid-triplet.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from rcm.ingestion.x12 import Limits, read_remittances, tokenize
from rcm.platform.errors import PlatformError

# Hypothesis' deadline is unhelpful here: the work is proportional to input
# size, and a large generated example is slow without being wrong.
_SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: Characters that actually turn up in EDI, weighted so that delimiters and
#: structural text appear far more often than random noise would produce.
_EDI_ALPHABET = st.sampled_from(list("ISACLPSVCBPRTRNDTMAMTLQGEIEAN1PLB0123456789.*:~^|>\r\n "))


def _well_formed_835(claims: int = 1, lines: int = 1) -> str:
    """A minimal but structurally valid 835, used as a mutation base."""
    body: list[str] = [
        "ST*835*0001~",
        "BPR*I*100.00*C*ACH*CCP*01*999999999*DA*1234*****20240131~",
        "TRN*1*TRACE1*9000~",
        "DTM*405*20240131~",
        "N1*PR*A Payer~",
        "N1*PE*A Payee*XX*1234567893~",
        "LX*1~",
    ]
    for claim in range(claims):
        body.append(f"CLP*CLM{claim}*1*200.00*100.00**MB*PCN{claim}*11~")
        for line in range(lines):
            body.append(f"SVC*HC:9921{line}*200.00*100.00**1~")
            body.append("CAS*CO*45*100.00~")
            body.append("AMT*B6*100.00~")
    segment_count = len(body) + 1  # + the SE about to be added
    body.append(f"SE*{segment_count}*0001~")

    isa = (
        "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
        "*240131*0000*^*00501*000000001*0*T*:~"
    )
    return (
        isa
        + "\n"
        + "GS*HP*SENDER*RECEIVER*20240131*0000*1*X*005010X221A1~\n"
        + "\n".join(body)
        + "\nGE*1*1~\nIEA*1*000000001~"
    )


# ---------------------------------------------------------------------------
# The headline property
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@_SETTINGS
@given(st.binary(max_size=4096))
def test_tokenizer_never_raises_an_unnamed_exception_on_arbitrary_bytes(raw: bytes) -> None:
    """Any byte sequence at all: parsed, or refused by a named error."""
    # A named refusal is the contract being satisfied, not a failure; anything
    # else propagates and fails the test, which is the whole point.
    with contextlib.suppress(PlatformError):
        tokenize(raw)


@pytest.mark.invariant
@_SETTINGS
@given(st.text(alphabet=_EDI_ALPHABET, max_size=4096))
def test_full_parse_never_raises_an_unnamed_exception_on_edi_shaped_text(text: str) -> None:
    """The same property through the whole stack, on input that looks like EDI.

    Random bytes mostly fail at the first check. Text drawn from the EDI
    alphabet gets further in, which is where the interesting failures are.
    """
    with contextlib.suppress(PlatformError):
        read_remittances(text)


@pytest.mark.invariant
@_SETTINGS
@given(
    st.integers(min_value=0, max_value=4000),
    st.integers(min_value=0, max_value=4000),
)
def test_truncation_at_any_offset_is_refused_cleanly(start: int, length: int) -> None:
    """Every prefix, suffix and slice of a valid file.

    A truncated transfer is the most common real-world corruption, and slicing
    a known-good file reaches structural states that random generation almost
    never produces -- a file ending mid-CAS, mid-composite, or between ST and
    SE.
    """
    valid = _well_formed_835(claims=2, lines=2)
    fragment = valid[start : start + length]
    assume(fragment != valid)
    with contextlib.suppress(PlatformError):
        read_remittances(fragment)


@pytest.mark.invariant
@_SETTINGS
@given(
    st.integers(min_value=0, max_value=1200),
    st.characters(min_codepoint=32, max_codepoint=126),
)
def test_single_character_corruption_is_refused_cleanly(index: int, char: str) -> None:
    """Flip one character anywhere in a valid file."""
    valid = _well_formed_835(claims=2, lines=2)
    assume(index < len(valid))
    corrupted = valid[:index] + char + valid[index + 1 :]
    assume(corrupted != valid)
    with contextlib.suppress(PlatformError):
        read_remittances(corrupted)


# ---------------------------------------------------------------------------
# Properties of a successful parse
# ---------------------------------------------------------------------------


@_SETTINGS
@given(st.integers(min_value=1, max_value=8), st.integers(min_value=1, max_value=8))
def test_every_claim_and_line_survives_the_round_trip(claims: int, lines: int) -> None:
    """Nothing is dropped, whatever the shape of the file.

    The failure this guards against is a loop cursor that resets in the wrong
    place -- the kind of defect that loses the last line of every claim, or
    every claim after the first, and still produces a plausible-looking result.
    """
    remittances = read_remittances(_well_formed_835(claims=claims, lines=lines))

    assert len(remittances) == 1
    remittance = remittances[0]
    assert len(remittance.claims) == claims
    for claim in remittance.claims:
        assert len(claim.service_lines) == lines
        for line in claim.service_lines:
            # One CAS reason per line in this fixture; losing it is the PD-6
            # failure, duplicating it is a cursor that never resets.
            assert len(line.adjustments) == 1
            assert line.adjustments[0].group == "CO"


@_SETTINGS
@given(st.sampled_from(["*", "|", "+"]), st.sampled_from(["~", "\x1c"]))
def test_delimiters_are_read_from_the_file_not_assumed(element: str, segment: str) -> None:
    """A sender's choice of delimiters is theirs, not ours.

    Hard-coding `*` and `~` works until the first payer who does not use them,
    and then fails on every file they send rather than on one.
    """
    text = _well_formed_835().replace("*", element).replace("~", segment)
    remittances = read_remittances(text)

    assert len(remittances) == 1
    assert remittances[0].claims[0].service_lines[0].adjustments[0].reason_code == "45"


@_SETTINGS
@given(st.booleans())
def test_line_breaks_between_segments_are_optional(newlines: bool) -> None:
    """X12 does not require them and real senders differ.

    A parser that depends on line breaks is broken; so is one that chokes when
    they are present.
    """
    text = _well_formed_835(claims=2, lines=2)
    if not newlines:
        text = text.replace("\n", "")

    remittances = read_remittances(text)
    assert len(remittances[0].claims) == 2


@_SETTINGS
@given(st.integers(min_value=1, max_value=6))
def test_cas_triplets_are_unrolled_one_row_per_reason(triplets: int) -> None:
    """PD-6, at every permitted triplet count.

    One CAS segment can carry six reasons. The parser must produce six rows,
    not one row and five discarded reasons.
    """
    amounts = [Decimal("10.00")] * triplets
    reasons = ["45", "97", "18", "29", "96", "16"][:triplets]
    cas = "CAS*CO" + "".join(f"*{r}*{a}*" for r, a in zip(reasons, amounts, strict=True))
    cas = cas.rstrip("*") + "~"

    charge = Decimal("10.00") * triplets
    body = [
        "ST*835*0001~",
        "BPR*I*0.00*C*ACH*CCP*01*9*DA*1*****20240131~",
        "TRN*1*T*9~",
        "N1*PR*P~",
        "N1*PE*Q*XX*1234567893~",
        "LX*1~",
        f"CLP*C1*4*{charge}*0.00**MB*PCN1*11~",
        f"SVC*HC:99213*{charge}*0.00**1~",
        cas,
        "AMT*B6*0.00~",
    ]
    body.append(f"SE*{len(body) + 1}*0001~")
    isa = (
        "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
        "*240131*0000*^*00501*000000001*0*T*:~"
    )
    text = (
        isa
        + "GS*HP*SENDER*RECEIVER*20240131*0000*1*X*005010X221A1~"
        + "".join(body)
        + "GE*1*1~IEA*1*000000001~"
    )

    line = read_remittances(text)[0].claims[0].service_lines[0]
    assert len(line.adjustments) == triplets
    assert [a.reason_code for a in line.adjustments] == reasons
    assert all(a.group == "CO" for a in line.adjustments)
    assert line.balances


@_SETTINGS
@given(st.integers(min_value=1, max_value=200))
def test_segment_limit_is_enforced_before_the_work_is_done(limit: int) -> None:
    """SR-12: a bound that is only checked afterwards is not a bound."""
    text = _well_formed_835(claims=20, lines=3)
    try:
        segments, _ = tokenize(text, limits=Limits(max_segments=limit))
    except PlatformError as exc:
        assert exc.code == "x12-limit-exceeded"
        return
    assert len(segments) <= limit
