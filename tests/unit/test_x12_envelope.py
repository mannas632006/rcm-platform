"""Envelope integrity: control numbers and counts (M1.1).

Each of these is a file telling you it is intact. They are cheap to check and
they catch the corruptions that otherwise surface much later as a claim list
that is quietly three claims short.

DR-10 requires a quarantined file to name the assertion it failed, so every
test here asserts on `context["assertion"]` and not merely that something was
raised. "Malformed file" is not an operator-actionable message.
"""

from __future__ import annotations

import pytest

from rcm.ingestion.x12 import Limits, read_interchange, tokenize
from rcm.platform.errors import MalformedX12Error, X12LimitExceededError

ISA = (
    "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
    "*240131*0000*^*00501*000000001*0*T*:~"
)


def interchange(
    *,
    body: str = "ST*835*0001~BPR*I*0.00*C*ACH*CCP*01*9*DA*1*****20240131~SE*3*0001~",
    gs06: str = "1",
    ge01: str = "1",
    ge02: str = "1",
    iea01: str = "1",
    iea02: str = "000000001",
    isa: str = ISA,
) -> str:
    return (
        f"{isa}"
        f"GS*HP*SENDER*RECEIVER*20240131*0000*{gs06}*X*005010X221A1~"
        f"{body}"
        f"GE*{ge01}*{ge02}~"
        f"IEA*{iea01}*{iea02}~"
    )


def test_a_well_formed_interchange_parses() -> None:
    result = read_interchange(interchange())

    assert result.sender_id == "SENDER"
    assert result.receiver_id == "RECEIVER"
    assert result.control_number == "000000001"
    assert result.is_production is False  # ISA15 = T
    assert len(result.transaction_sets("835")) == 1


def test_production_and_test_interchanges_are_distinguishable() -> None:
    """A test file posted as production is money moved against a pretend remittance."""
    assert read_interchange(interchange()).is_production is False
    assert read_interchange(interchange(isa=ISA.replace("*0*T*:~", "*0*P*:~"))).is_production


@pytest.mark.parametrize(
    ("kwargs", "assertion"),
    [
        ({"iea02": "000000999"}, "IEA02 == ISA13"),
        ({"ge02": "77"}, "GE02 == GS06"),
        ({"ge01": "5"}, "GE01 == count(ST)"),
        ({"iea01": "4"}, "IEA01 == count(GS)"),
        (
            {"body": "ST*835*0001~BPR*I*0.00*C*A*C*01*9*DA*1*****20240131~SE*99*0001~"},
            "SE01 == count(ST..SE)",
        ),
        (
            {"body": "ST*835*0001~BPR*I*0.00*C*A*C*01*9*DA*1*****20240131~SE*3*9999~"},
            "SE02 == ST02",
        ),
    ],
)
def test_every_control_mismatch_names_its_assertion(kwargs: dict[str, str], assertion: str) -> None:
    with pytest.raises(MalformedX12Error) as caught:
        read_interchange(interchange(**kwargs))

    assert caught.value.context["assertion"] == assertion


def test_control_numbers_compare_numerically_not_textually() -> None:
    """`000000001` and `1` are the same control number.

    Senders pad inconsistently, and the standard's own examples do too.
    Comparing literal strings rejects files that are correct.
    """
    assert read_interchange(interchange(iea02="1")).control_number == "000000001"


def test_a_transaction_without_its_trailer_is_refused() -> None:
    with pytest.raises(MalformedX12Error) as caught:
        read_interchange(interchange(body="ST*835*0001~BPR*I*0.00*C*A*C*01*9*DA*1~"))

    assert "every ST has an SE" in str(caught.value.context.get("assertion", ""))


def test_se_without_st_is_refused() -> None:
    with pytest.raises(MalformedX12Error):
        read_interchange(interchange(body="SE*3*0001~"))


def test_a_file_that_is_not_x12_is_refused_by_name() -> None:
    for payload in (b"", b"<?xml version='1.0'?><claim/>", b"ISA", b"not edi at all"):
        with pytest.raises(MalformedX12Error):
            read_interchange(payload)


def test_delimiters_come_from_the_isa_not_from_assumption() -> None:
    text = interchange().replace("*", "|").replace("~", "\x1c")
    result = read_interchange(text)

    assert result.separators.element == "|"
    assert result.separators.segment == "\x1c"
    assert len(result.transaction_sets("835")) == 1


def test_colliding_delimiters_are_refused() -> None:
    """If the element and segment separators are the same, nothing is parseable."""
    broken = ISA[:105] + "*"  # segment terminator equal to the element separator
    with pytest.raises(MalformedX12Error):
        tokenize(broken + "GS*HP*S*R*20240131*0000*1*X*005010X221A1*")


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        (Limits(max_bytes=10), "max_bytes"),
        (Limits(max_segments=2), "max_segments"),
        (Limits(max_elements_per_segment=3), "max_elements_per_segment"),
    ],
)
def test_each_limit_is_enforced_and_names_itself(limits: Limits, expected: str) -> None:
    """SR-12. A limit that fires without saying which one is not diagnosable."""
    with pytest.raises(X12LimitExceededError) as caught:
        tokenize(interchange(), limits=limits)

    assert caught.value.context["limit"] == expected


def test_segments_carry_their_position_for_quarantine_reports() -> None:
    segments, _ = tokenize(interchange())

    assert segments[0].tag == "ISA"
    assert segments[0].position == 1
    assert [s.position for s in segments] == list(range(1, len(segments) + 1))


def test_elements_are_one_based_to_match_the_specification() -> None:
    """`CLP01` is `get(1)`. Reading a spec that counts from one while indexing
    from zero is where off-by-one defects come from."""
    segments, _ = tokenize(interchange())
    isa = segments[0]

    assert isa.get(1) == "00"
    assert isa.get(13) == "000000001"
    assert isa.get(99) == ""  # absent elements read as empty, never raise
    with pytest.raises(ValueError, match="one-based"):
        isa.get(0)
