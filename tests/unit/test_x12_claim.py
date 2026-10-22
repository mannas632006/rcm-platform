"""837P parser and generator (M1.5).

Fixture-driven, which is the weaker half of M1.5's evidence. The stronger half
is `tests/unit/test_x12_claim_corpus.py`, which checks the parser against 325
files written by an independent generator, and
`tests/property/test_x12_claim_roundtrip.py`, which asserts the round trip over
arbitrary claims rather than the ones that occurred to the author. A fixture
only ever contains what someone already thought of.

What is here is the set of behaviours that are easier to pin down one at a
time: the refusals, the fields that are easy to drop, and the two prime
directives this module carries.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from rcm.ingestion.x12 import read_claims, read_interchange
from rcm.ingestion.x12.claim import (
    BillingProvider,
    Claim837,
    ClaimServiceLine,
    ClaimSubmission,
    Subscriber,
    TradingPartner,
    build_837,
    parse_837,
)
from rcm.platform.errors import MalformedX12Error, PlatformError, UnsupportedX12Error


def isa(sender: str = "SUBMITTER01", receiver: str = "CLEARINGHSE1") -> str:
    """Assembled from parts, never substituted into a literal.

    ISA is fixed-width and every delimiter in the file is read back out of it
    by offset, so a single character of drift makes the whole file
    unparseable -- and the resulting failures look like parser bugs anywhere
    but here.
    """
    return (
        "ISA*00*"
        + " " * 10
        + "*00*"
        + " " * 10
        + "*ZZ*"
        + sender.ljust(15)
        + "*ZZ*"
        + receiver.ljust(15)
        + "*240101*0000*^*00501*000100026*0*T*:~"
    )


def build_837_text(
    *,
    claim_segments: list[str] | None = None,
    hl_subscriber_level: str = "22",
    control: str = "0026",
) -> bytes:
    """A structurally valid 837P the caller can deliberately break."""
    body = [
        f"ST*837*{control}*005010X222A1~",
        f"BHT*0019*00*{control}*20240101*0000*CH~",
        "NM1*41*2*RCM SEED BILLING*****46*SUBMITTER01~",
        "PER*IC*OPS*TE*5125550100~",
        "NM1*40*2*SEED CLEARINGHOUSE*****46*CLEARINGHSE1~",
        "HL*1**20*1~",
        "PRV*BI*PXC*207Q00000X~",
        "NM1*85*2*Lakeview Clinic*****XX*4952654158~",
        "N3*7860 Maple Ave~",
        "N4*San Antonio*TX*78205~",
        "REF*EI*549656931~",
        f"HL*2*1*{hl_subscriber_level}*0~",
        "SBR*P*18*******CI~",
        "NM1*IL*1*Castellanos*Andre****MI*VAZ957419823~",
        "N3*4235 Pine Way~",
        "N4*San Antonio*TX*78205~",
        "DMG*D8*19640111*F~",
        "NM1*PR*2*BCBS Texas*****PI*00590~",
        *(
            claim_segments
            if claim_segments is not None
            else [
                "CLM*CLM00000055*186.49***11:B:1*Y*A*Y*Y~",
                "DTP*434*D8*20240101~",
                "HI*ABK:J06.9*ABF:E11.65~",
                "LX*1~",
                "SV1*HC:99203*186.49*UN*1***1:2~",
                "DTP*472*D8*20240101~",
            ]
        ),
    ]
    body.append(f"SE*{len(body) + 1}*{control}~")
    return (
        isa()
        + f"GS*HC*SUBMITTER01*CLEARINGHSE1*20240101*0000*{control}*X*005010X222A1~"
        + "".join(body)
        + f"GE*1*{control}~IEA*1*000100026~"
    ).encode()


def one_claim(payload: bytes) -> Claim837:
    submissions = read_claims(payload)
    assert len(submissions) == 1
    assert len(submissions[0].claims) == 1
    return submissions[0].claims[0]


# ---------------------------------------------------------------------------
# Reading what the file says
# ---------------------------------------------------------------------------


def test_the_claim_header_is_read() -> None:
    claim = one_claim(build_837_text())

    assert claim.claim_id == "CLM00000055"
    assert claim.total_charge == Decimal("186.49")
    assert claim.place_of_service == "11"
    assert claim.claim_frequency_code == "1"
    assert claim.payer_id == "00590"
    assert claim.payer_name == "BCBS Texas"
    assert claim.claim_filing_code == "CI"
    assert claim.payer_responsibility == "P"
    assert claim.service_date == date(2024, 1, 1)


def test_the_billing_provider_and_subscriber_do_not_swap_addresses() -> None:
    """N3 and N4 appear in both loops and mean different things in each.

    Dispatching on the tag alone puts the clinic's street address on the
    patient, which is a PHI error that produces a perfectly well-formed claim.
    """
    claim = one_claim(build_837_text())

    assert claim.billing_provider == BillingProvider(
        name="Lakeview Clinic",
        npi="4952654158",
        taxonomy_code="207Q00000X",
        address="7860 Maple Ave",
        city="San Antonio",
        state="TX",
        postal_code="78205",
        tax_id="549656931",
    )
    assert claim.subscriber == Subscriber(
        last_name="Castellanos",
        first_name="Andre",
        member_id="VAZ957419823",
        address="4235 Pine Way",
        city="San Antonio",
        state="TX",
        postal_code="78205",
        date_of_birth=date(1964, 1, 11),
        sex="F",
    )


def test_diagnoses_keep_their_order() -> None:
    """Pointers are one-based indexes into this list.

    Sorting or de-duplicating the diagnoses silently repoints every service
    line on the claim at a different code.
    """
    claim = one_claim(build_837_text())
    assert claim.diagnoses == ("J06.9", "E11.65")


def test_the_service_line_is_read_including_its_pointers() -> None:
    claim = one_claim(build_837_text())
    assert claim.lines == (
        ClaimServiceLine(
            line_number=1,
            procedure_code="99203",
            charge_amount=Decimal("186.49"),
            modifiers=(),
            unit_basis="UN",
            units=Decimal("1"),
            diagnosis_pointers=(1, 2),
            service_date=date(2024, 1, 1),
        ),
    )


def test_modifiers_are_read_in_the_order_the_file_carries_them() -> None:
    """Modifier order is meaningful: pricing modifiers precede informational
    ones (DOC-02 section 11), so a parser that sorts them changes the claim.

    The modifiers here are deliberately *not* in sorted order. The first
    version of this test used `25:59`, which sorts to itself, so a parser that
    sorted its modifiers passed it -- and the synthetic corpus turns out to
    contain no line with two modifiers at all, so nothing else was covering
    the order either. A fixture that cannot fail is worse than no fixture,
    because it reads like coverage.
    """
    claim = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM1*300.00***11:B:1*Y*A*Y*Y~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:99213:59:25*300.00*UN*1***1~",
            ]
        )
    )
    assert claim.lines[0].modifiers == ("59", "25")


def test_modifier_order_survives_the_round_trip() -> None:
    """The generator transcribes the order it is given, rather than tidying it."""
    original = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM1*300.00***11:B:1*Y*A*Y*Y~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:99213:59:25:76*300.00*UN*1***1~",
            ]
        )
    )
    assert original.lines[0].modifiers == ("59", "25", "76")

    rebuilt = read_claims(build_837(_submission((original,))).encode())[0].claims[0]
    assert rebuilt.lines[0].modifiers == ("59", "25", "76")


def test_fractional_units_are_not_truncated() -> None:
    """Anaesthesia minutes and drug units are legitimately fractional.

    Reading SV104 as an integer bills 1.5 units of a drug as 1.
    """
    claim = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM1*300.00***11:B:1*Y*A*Y*Y~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:J1100*300.00*UN*1.5***1~",
            ]
        )
    )
    assert claim.lines[0].units == Decimal("1.5")


def test_several_claims_in_one_transaction_are_all_read() -> None:
    """A batch holds many subscriber loops under one billing provider."""
    payload = (
        isa()
        + "GS*HC*SUBMITTER01*CLEARINGHSE1*20240101*0000*26*X*005010X222A1~"
        + "ST*837*0026*005010X222A1~"
        + "BHT*0019*00*0026*20240101*0000*CH~"
        + "NM1*41*2*S*****46*SUBMITTER01~"
        + "NM1*40*2*R*****46*CLEARINGHSE1~"
        + "HL*1**20*1~"
        + "NM1*85*2*Clinic*****XX*4952654158~"
        + "HL*2*1*22*0~"
        + "SBR*P*18*******CI~"
        + "NM1*IL*1*One*A****MI*M1~"
        + "NM1*PR*2*Payer*****PI*00590~"
        + "CLM*CLM1*100.00***11:B:1*Y*A*Y*Y~"
        + "HI*ABK:J06.9~"
        + "LX*1~"
        + "SV1*HC:99213*100.00*UN*1***1~"
        + "HL*3*1*22*0~"
        + "SBR*P*18*******CI~"
        + "NM1*IL*1*Two*B****MI*M2~"
        + "NM1*PR*2*Payer*****PI*00590~"
        + "CLM*CLM2*200.00***11:B:1*Y*A*Y*Y~"
        + "HI*ABK:E11.65~"
        + "LX*1~"
        + "SV1*HC:99214*200.00*UN*1***1~"
        + "SE*23*0026~GE*1*26~IEA*1*000100026~"
    ).encode()

    submission = read_claims(payload)[0]
    assert [c.claim_id for c in submission.claims] == ["CLM1", "CLM2"]
    # Each claim keeps its own subscriber rather than the last one seen.
    assert [c.subscriber.member_id for c in submission.claims] == ["M1", "M2"]
    assert [c.diagnoses for c in submission.claims] == [("J06.9",), ("E11.65",)]


# ---------------------------------------------------------------------------
# PD-7: resubmissions chain
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_resubmission_chains_to_original() -> None:
    """PD-7. REF*F8 names the claim this one replaces.

    Losing it makes three submissions of one claim count as three claims, and
    the denial rate falls by exactly the amount that matters -- the claims that
    had to be sent again are the denied ones.
    """
    claim = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM00000999*186.49***11:B:7*Y*A*Y*Y~",
                "REF*F8*CLM00000055~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:99203*186.49*UN*1***1~",
            ]
        )
    )
    assert claim.original_claim_id == "CLM00000055"
    assert claim.is_resubmission is True
    assert claim.claim_frequency_code == "7"


def test_an_original_claim_is_not_a_resubmission() -> None:
    claim = one_claim(build_837_text())
    assert claim.original_claim_id == ""
    assert claim.is_resubmission is False


def test_resubmission_is_read_from_the_reference_not_the_frequency_code() -> None:
    """The two disagree in practice. Senders set frequency 7 with no reference
    and send a reference with frequency 1; only the reference names a specific
    prior claim, so only the reference can chain."""
    frequency_only = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM1*100.00***11:B:7*Y*A*Y*Y~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:99213*100.00*UN*1***1~",
            ]
        )
    )
    assert frequency_only.claim_frequency_code == "7"
    assert frequency_only.is_resubmission is False

    reference_only = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM1*100.00***11:B:1*Y*A*Y*Y~",
                "REF*F8*CLM0~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:99213*100.00*UN*1***1~",
            ]
        )
    )
    assert reference_only.claim_frequency_code == "1"
    assert reference_only.is_resubmission is True


def test_the_other_claim_references_are_kept_apart() -> None:
    """G1, 9F and F8 all arrive as REF in loop 2300 and mean different things.
    Reading any of them as the resubmission chain invents a resubmission."""
    claim = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM1*100.00***11:B:1*Y*A*Y*Y~",
                "REF*G1*AUTH123~",
                "REF*9F*REFERRAL9~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:99213*100.00*UN*1***1~",
            ]
        )
    )
    assert claim.prior_auth_number == "AUTH123"
    assert claim.referral_number == "REFERRAL9"
    assert claim.original_claim_id == ""
    assert claim.is_resubmission is False


def test_a_billing_provider_reference_is_not_a_claim_reference() -> None:
    """REF*EI sits in the billing provider loop. Reading REF without tracking
    which loop it is in puts the tax ID somewhere on the claim."""
    claim = one_claim(build_837_text())
    assert claim.billing_provider.tax_id == "549656931"


# ---------------------------------------------------------------------------
# BR-M6-01: reported, not judged
# ---------------------------------------------------------------------------


def test_a_line_without_a_diagnosis_pointer_is_reported() -> None:
    """BR-M6-01 says every line must carry at least one pointer.

    This module reports rather than raises, exactly as the 835 parser reports
    `balances` rather than quarantining: saying what a file contains and
    deciding what that means are different jobs, and keeping them apart is what
    lets a reconciliation tool read a broken file deliberately.
    """
    claim = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM1*300.00***11:B:1*Y*A*Y*Y~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:99213*100.00*UN*1***1~",
                "LX*2~",
                "SV1*HC:99214*200.00*UN*1~",
            ]
        )
    )
    assert claim.lines_without_diagnosis_pointer == (2,)
    assert claim.lines[0].diagnosis_pointers == (1,)
    assert claim.lines[1].diagnosis_pointers == ()


def test_a_claim_whose_lines_do_not_sum_to_its_total_is_reported() -> None:
    claim = one_claim(
        build_837_text(
            claim_segments=[
                "CLM*CLM1*500.00***11:B:1*Y*A*Y*Y~",
                "HI*ABK:J06.9~",
                "LX*1~",
                "SV1*HC:99213*100.00*UN*1***1~",
            ]
        )
    )
    assert claim.charges_reconcile is False
    assert claim.line_charge_total == Decimal("100.00")


# ---------------------------------------------------------------------------
# Refusals, each named
# ---------------------------------------------------------------------------


def test_a_dependent_loop_is_refused_rather_than_guessed() -> None:
    """HL*23 means the patient is not the subscriber.

    The demographics would come from loop 2010CA, which this parser does not
    read. Borrowing the subscriber's would produce a well-formed claim about
    the wrong patient -- an error no later stage could detect, because nothing
    about the result looks wrong. Failing closed and naming the gap is the only
    honest outcome (PD-18).
    """
    with pytest.raises(UnsupportedX12Error) as caught:
        read_claims(build_837_text(hl_subscriber_level="23"))

    assert "dependent" in str(caught.value.context.get("reason", "")).lower()
    # Distinct from malformed: the file is correct X12 and the gap is ours.
    assert not isinstance(caught.value, MalformedX12Error)


def test_an_unknown_hierarchical_level_is_refused() -> None:
    with pytest.raises(MalformedX12Error):
        read_claims(build_837_text(hl_subscriber_level="99"))


def test_a_charge_that_is_not_a_number_is_refused_rather_than_defaulted() -> None:
    """A zero substituted here is a wrong number in a ledger, invisibly."""
    with pytest.raises(MalformedX12Error):
        read_claims(
            build_837_text(
                claim_segments=[
                    "CLM*CLM1*not-a-number***11:B:1*Y*A*Y*Y~",
                    "HI*ABK:J06.9~",
                    "LX*1~",
                    "SV1*HC:99213*100.00*UN*1***1~",
                ]
            )
        )


def test_a_diagnosis_pointer_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(MalformedX12Error):
        read_claims(
            build_837_text(
                claim_segments=[
                    "CLM*CLM1*100.00***11:B:1*Y*A*Y*Y~",
                    "HI*ABK:J06.9~",
                    "LX*1~",
                    "SV1*HC:99213*100.00*UN*1***X~",
                ]
            )
        )


def test_a_service_line_outside_any_lx_loop_is_refused() -> None:
    with pytest.raises(MalformedX12Error):
        read_claims(
            build_837_text(
                claim_segments=[
                    "CLM*CLM1*100.00***11:B:1*Y*A*Y*Y~",
                    "HI*ABK:J06.9~",
                    "SV1*HC:99213*100.00*UN*1***1~",
                ]
            )
        )


def test_an_sv101_without_a_procedure_code_is_refused() -> None:
    with pytest.raises(MalformedX12Error):
        read_claims(
            build_837_text(
                claim_segments=[
                    "CLM*CLM1*100.00***11:B:1*Y*A*Y*Y~",
                    "HI*ABK:J06.9~",
                    "LX*1~",
                    "SV1*HC*100.00*UN*1***1~",
                ]
            )
        )


def test_a_line_number_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(MalformedX12Error):
        read_claims(
            build_837_text(
                claim_segments=[
                    "CLM*CLM1*100.00***11:B:1*Y*A*Y*Y~",
                    "HI*ABK:J06.9~",
                    "LX*one~",
                    "SV1*HC:99213*100.00*UN*1***1~",
                ]
            )
        )


def test_an_impossible_date_is_refused() -> None:
    with pytest.raises(MalformedX12Error):
        read_claims(
            build_837_text(
                claim_segments=[
                    "CLM*CLM1*100.00***11:B:1*Y*A*Y*Y~",
                    "DTP*434*D8*20240230~",
                    "HI*ABK:J06.9~",
                    "LX*1~",
                    "SV1*HC:99213*100.00*UN*1***1~",
                ]
            )
        )


def test_every_refusal_is_a_platform_error() -> None:
    """DR-10 again: a quarantined file must name the assertion it failed, and
    an unnamed exception names nothing."""
    for broken in (
        b"",
        b"ISA*not an interchange~",
        build_837_text(hl_subscriber_level="23"),
        build_837_text(claim_segments=["CLM*C*x***11:B:1~"]),
    ):
        with pytest.raises(PlatformError):
            read_claims(broken)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _submission(claims: tuple[Claim837, ...]) -> ClaimSubmission:
    return ClaimSubmission(
        control_number="0026",
        submitter=TradingPartner(name="RCM SEED BILLING", etin="SUBMITTER01"),
        receiver=TradingPartner(name="SEED CLEARINGHOUSE", etin="CLEARINGHSE1"),
        claims=claims,
        created_on=date(2024, 1, 1),
    )


def _claim(**overrides: object) -> Claim837:
    values: dict[str, object] = {
        "claim_id": "CLM1",
        "total_charge": Decimal("186.49"),
        "billing_provider": BillingProvider(name="Clinic", npi="4952654158"),
        "subscriber": Subscriber(last_name="A", first_name="B", member_id="M1"),
        "payer_id": "00590",
        "payer_name": "A Payer",
        "claim_filing_code": "CI",
        "place_of_service": "11",
        "diagnoses": ("J06.9",),
        "lines": (
            ClaimServiceLine(
                line_number=1,
                procedure_code="99203",
                charge_amount=Decimal("186.49"),
                diagnosis_pointers=(1,),
            ),
        ),
    }
    values.update(overrides)
    return Claim837(**values)  # type: ignore[arg-type]


def test_a_generated_file_parses_as_a_valid_interchange() -> None:
    """The envelope counts have to be right or nothing downstream reads it."""
    text = build_837(_submission((_claim(),)))
    interchange = read_interchange(text)

    assert len(interchange.functional_groups) == 1
    transaction = interchange.transaction_sets("837")[0]
    # SE01 counts ST through SE inclusive; the envelope parser asserts it.
    assert transaction.declared_segment_count == transaction.actual_segment_count


def test_the_generated_isa_is_exactly_106_characters() -> None:
    """Every delimiter is read back out of the ISA by offset, so its width is
    load-bearing rather than cosmetic."""
    text = build_837(_submission((_claim(),)))
    isa_segment = text[: text.index("~") + 1]
    assert len(isa_segment) == 106
    assert isa_segment[3] == "*"
    assert isa_segment[82] == "^"
    assert isa_segment[104] == ":"
    assert isa_segment[105] == "~"


@pytest.mark.invariant
def test_modifier_25_never_auto_applied() -> None:
    """PD-16. The generator transcribes modifiers and never supplies one."""
    text = build_837(_submission((_claim(),)))
    assert ":25" not in text
    parsed = parse_837(read_interchange(text).transaction_sets("837")[0], _separators(text))
    assert parsed.claims[0].lines[0].modifiers == ()


@pytest.mark.invariant
def test_modifier_59_never_auto_applied() -> None:
    """PD-16, the other half."""
    text = build_837(_submission((_claim(),)))
    assert ":59" not in text


def test_a_line_without_a_pointer_is_written_without_one() -> None:
    """The generator does not invent a diagnosis pointer to satisfy BR-M6-01.

    Inventing one would hide the defect from the validator, which is the only
    thing that can catch it before the payer does.
    """
    claim = _claim(
        lines=(
            ClaimServiceLine(
                line_number=1,
                procedure_code="99203",
                charge_amount=Decimal("186.49"),
                diagnosis_pointers=(),
            ),
        )
    )
    text = build_837(_submission((claim,)))
    reparsed = read_claims(text.encode())[0].claims[0]
    assert reparsed.lines_without_diagnosis_pointer == (1,)


def test_claims_from_different_billing_providers_get_their_own_loops() -> None:
    """A parser reads the billing provider from the enclosing 2000A loop, so a
    generator emitting one loop per file would silently move every claim onto
    the first claim's provider."""
    first = _claim(claim_id="CLM1", billing_provider=BillingProvider(name="One", npi="1"))
    second = _claim(claim_id="CLM2", billing_provider=BillingProvider(name="Two", npi="2"))

    text = build_837(_submission((first, second)))
    reparsed = read_claims(text.encode())[0].claims

    assert [c.billing_provider.name for c in reparsed] == ["One", "Two"]
    assert text.count("*20*1~") == 2


def test_consecutive_claims_sharing_a_provider_share_one_loop() -> None:
    provider = BillingProvider(name="One", npi="1")
    text = build_837(
        _submission(
            (
                _claim(claim_id="CLM1", billing_provider=provider),
                _claim(claim_id="CLM2", billing_provider=provider),
            )
        )
    )
    assert text.count("*20*1~") == 1
    assert len(read_claims(text.encode())[0].claims) == 2


def _separators(text: str) -> object:
    """The delimiters a generated file declares, read back out of it."""
    return read_interchange(text).separators
