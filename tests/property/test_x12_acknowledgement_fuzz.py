"""Fuzz and classification properties for the acknowledgement parsers (M1.6).

Two kinds of property, answering two different worries.

**Nothing gets through uncaught.** CLAUDE.md section 7.1: *"Parsers: no
uncaught exception on any input."* These files arrive from a clearinghouse
over a channel this platform does not control, so they are untrusted input
(S-12). A named refusal is the contract being met; a `ValueError` escaping
from the middle of a parse is a file that is neither loaded nor quarantined,
which breaks PD-9 and DR-10 together.

**The verdict is total and unambiguous.** Every status code is accepted, or
rejected, or recognised as neither -- exactly one of the three, for every
possible code, not just the ones in the fixtures. A code that reads as both
accepted and rejected would resolve to whichever branch the caller happened to
test first, and that is the kind of bug that produces a number nobody can
explain six months later.
"""

from __future__ import annotations

import contextlib

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rcm.ingestion.x12 import read_acknowledgements, read_claim_statuses, read_eligibility
from rcm.ingestion.x12.acknowledgement import ACCEPTED_CODES, REJECTED_CODES
from rcm.ingestion.x12.claim_status import (
    ACCEPTED_CATEGORIES,
    REJECTED_CATEGORIES,
    ClaimStatus,
)
from rcm.ingestion.x12.eligibility import (
    ACTIVE_COVERAGE_CODES,
    INACTIVE_COVERAGE_CODES,
    Benefit,
)
from rcm.platform.errors import PlatformError

_SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

_EDI_ALPHABET = st.sampled_from(
    list("ISAGSSTAK1AK2AK9IK3IK4IK5BHTHLNM1TRNSTCREFDTPEBAAADMG0123456789.*:~^|>-\r\n ")
)

_ISA = (
    "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
    "*240131*0000*^*00501*000000001*0*T*:~"
)

READERS = (read_acknowledgements, read_claim_statuses, read_eligibility)


def _wrap(body: list[str], *, functional: str, version: str) -> str:
    segments = [*body, f"SE*{len(body) + 1}*0001~"]
    return (
        _ISA
        + f"GS*{functional}*SENDER*RECEIVER*20240131*0000*1*X*{version}~"
        + "".join(segments)
        + "GE*1*1~IEA*1*000000001~"
    )


def well_formed_999() -> str:
    return _wrap(
        [
            "ST*999*0001*005010X231A1~",
            "AK1*HC*100072*005010X222A1~",
            "AK2*837*0001*005010X222A1~",
            "IK3*CLM*23*2300*8~",
            "IK4*2*782*7*NOTANUMBER~",
            "IK5*R*5~",
            "AK9*R*1*1*0~",
        ],
        functional="FA",
        version="005010X231A1",
    )


def well_formed_277() -> str:
    return _wrap(
        [
            "ST*277*0001*005010X214~",
            "BHT*0085*08*0001*20240131*0000*TH~",
            "HL*1**20*1~",
            "NM1*PR*2*A Payer*****PI*00590~",
            "HL*4*1*22*0~",
            "NM1*QC*1*Castellanos*Andre~",
            "TRN*2*CLM1~",
            "STC*A3:21*20240131*WQ*200.00********Returned~",
            "REF*1K*PCNCLM1~",
        ],
        functional="HN",
        version="005010X214",
    )


def well_formed_271() -> str:
    return _wrap(
        [
            "ST*271*0001*005010X279A1~",
            "BHT*0022*11*0001*20240131*0000~",
            "HL*1**20*1~",
            "NM1*PR*2*A Payer*****PI*00590~",
            "HL*2*1*22*0~",
            "NM1*IL*1*Castellanos*Andre****MI*VAZ957419823~",
            "EB*1*IND*30**Gold PPO~",
            "DTP*291*RD8*20240101-20241231~",
        ],
        functional="HB",
        version="005010X279A1",
    )


WELL_FORMED = (well_formed_999, well_formed_277, well_formed_271)


# ---------------------------------------------------------------------------
# Nothing gets through uncaught
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@_SETTINGS
@given(st.binary(max_size=4096))
def test_no_acknowledgement_reader_raises_an_unnamed_exception_on_arbitrary_bytes(
    raw: bytes,
) -> None:
    for read in READERS:
        with contextlib.suppress(PlatformError):
            read(raw)


@pytest.mark.invariant
@_SETTINGS
@given(st.text(alphabet=_EDI_ALPHABET, max_size=4096))
def test_no_acknowledgement_reader_raises_an_unnamed_exception_on_edi_shaped_text(
    text: str,
) -> None:
    """Random bytes mostly fail at the first check; text from the EDI alphabet
    gets as far as the segment loops, which is where the interesting failures
    are."""
    for read in READERS:
        with contextlib.suppress(PlatformError):
            read(text)


@pytest.mark.invariant
@_SETTINGS
@given(
    st.integers(min_value=0, max_value=2),
    st.integers(min_value=0, max_value=2000),
    st.sampled_from(list("*~:0123456789ABZ ")),
)
def test_single_character_corruption_is_refused_cleanly(which: int, index: int, char: str) -> None:
    """One flipped character anywhere in a valid file.

    This is the realistic corruption -- a truncated transfer, a mangled
    encoding -- and it is the case where a parser is most likely to carry on
    with a half-read value rather than stop.
    """
    source = WELL_FORMED[which]()
    # Taken modulo the length rather than discarded when out of range: these
    # files are a few hundred characters, so an unbounded index would skip
    # most examples and the property would pass by not running.
    at = index % len(source)
    corrupted = source[:at] + char + source[at + 1 :]

    with contextlib.suppress(PlatformError):
        READERS[which](corrupted)


@pytest.mark.invariant
@_SETTINGS
@given(st.integers(min_value=0, max_value=2), st.integers(min_value=0, max_value=2000))
def test_truncation_at_any_offset_is_refused_cleanly(which: int, cut: int) -> None:
    source = WELL_FORMED[which]()
    with contextlib.suppress(PlatformError):
        READERS[which](source[: cut % (len(source) + 1)])


# ---------------------------------------------------------------------------
# The verdict is total and unambiguous
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=90), max_size=4))
def test_a_claim_status_is_never_both_accepted_and_rejected(category: str) -> None:
    """PD-4 at the level of a single code.

    A code on both lists would resolve to whichever branch the caller tested
    first, so the same 277CA would produce a rejection or an acceptance
    depending on which query ran -- and both answers would look right.
    """
    status = ClaimStatus(claim_id="C1", category_code=category)
    assert not (status.accepted and status.rejected)
    assert status.is_known_category == (status.accepted or status.rejected)


@pytest.mark.invariant
@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=90), max_size=4))
def test_a_benefit_is_never_both_active_and_inactive(code: str) -> None:
    benefit = Benefit(code=code)
    assert not (benefit.is_active and benefit.is_inactive)
    assert benefit.states_coverage == (benefit.is_active or benefit.is_inactive)


def test_the_code_lists_do_not_overlap() -> None:
    """Stated once as a set property, so extending a list cannot quietly
    create an ambiguous code."""
    assert not (ACCEPTED_CODES & REJECTED_CODES)
    assert not (ACCEPTED_CATEGORIES & REJECTED_CATEGORIES)
    assert not (ACTIVE_COVERAGE_CODES & INACTIVE_COVERAGE_CODES)


@pytest.mark.invariant
@given(
    st.lists(
        st.sampled_from(sorted(ACCEPTED_CATEGORIES | REJECTED_CATEGORIES | {"ZZ", "QQ"})),
        min_size=1,
        max_size=6,
    )
)
def test_every_status_lands_in_exactly_one_bucket(categories: list[str]) -> None:
    """Across a whole response: accepted + rejected + unknown accounts for
    every status, with nothing counted twice and nothing lost."""
    body = [
        "ST*277*0001*005010X214~",
        "BHT*0085*08*0001*20240131*0000*TH~",
        "HL*1**20*1~",
        "NM1*PR*2*A Payer*****PI*00590~",
    ]
    for position, category in enumerate(categories):
        body.extend(
            [
                f"HL*{position + 4}*1*22*0~",
                f"TRN*2*CLM{position}~",
                f"STC*{category}:20*20240131*WQ~",
            ]
        )
    response = read_claim_statuses(_wrap(body, functional="HN", version="005010X214"))[0]

    assert len(response.statuses) == len(categories)
    unknown = [s for s in response.statuses if not s.is_known_category]
    assert len(response.accepted_claims) + len(response.rejected_claims) + len(unknown) == len(
        categories
    )
    # `unknown_categories` is what the loader refuses on, so it must be
    # non-empty exactly when an unreadable status is present.
    assert bool(response.unknown_categories) == bool(unknown)
