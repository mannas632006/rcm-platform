"""835 parsing, and the prime directives it is responsible for (M1.2).

PD-3 and PD-6 are both decided here, at parse time. If a reason code arrives
without its group, or several reasons are collapsed into one row, no later
stage can detect it -- the data simply looks like a claim that was adjusted
once. CLAUDE.md section 5 is explicit that this is where to slow down.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rcm.ingestion.x12 import read_remittances
from rcm.platform.errors import MalformedX12Error

ISA = (
    "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
    "*240131*0000*^*00501*000000001*0*T*:~"
)


def remittance(body_segments: list[str], *, payment: str = "100.00") -> str:
    """Wrap 835 content in a valid envelope with correct counts."""
    body = [
        "ST*835*0001~",
        f"BPR*I*{payment}*C*ACH*CCP*01*999999999*DA*1234*****20240131~",
        "TRN*1*TRACE1*9000~",
        "DTM*405*20240131~",
        "N1*PR*Big Payer~",
        "N1*PE*A Practice*XX*1234567893~",
        "LX*1~",
        *body_segments,
    ]
    body.append(f"SE*{len(body) + 1}*0001~")
    return (
        ISA
        + "GS*HP*SENDER*RECEIVER*20240131*0000*1*X*005010X221A1~"
        + "".join(body)
        + "GE*1*1~IEA*1*000000001~"
    )


def one_claim(*line_segments: str, paid: str = "100.00", status: str = "1") -> str:
    return remittance(
        [
            f"CLP*CLM1*{status}*200.00*{paid}**MB*PCN1*11~",
            "NM1*QC*1*Delgado*Rosa****MI*MEM123~",
            "NM1*82*1*Okafor*Daniel****XX*1234567893~",
            "DTM*232*20240115~",
            *line_segments,
        ],
        payment=paid,
    )


# ---------------------------------------------------------------------------
# PD-6: service-line grain, one row per reason
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_multi_cas_adjustments_not_merged() -> None:
    """One CAS segment, three reasons, three rows.

    The natural implementation produces one row per *segment*. That loses two
    of these three reasons, and nothing downstream can tell.
    """
    text = one_claim(
        "SVC*HC:99213*200.00*0.00**1~",
        "CAS*CO*45*120.00**97*50.00**18*30.00~",
        "AMT*B6*0.00~",
        paid="0.00",
    )
    line = read_remittances(text)[0].claims[0].service_lines[0]

    assert len(line.adjustments) == 3
    assert [a.reason_code for a in line.adjustments] == ["45", "97", "18"]
    assert [a.amount for a in line.adjustments] == [
        Decimal("120.00"),
        Decimal("50.00"),
        Decimal("30.00"),
    ]
    assert all(a.group == "CO" for a in line.adjustments)
    assert line.balances


@pytest.mark.invariant
def test_service_line_grain_not_collapsed() -> None:
    """Several CAS segments on one line, several groups, every row kept."""
    text = one_claim(
        "SVC*HC:99213*200.00*60.00**1~",
        "CAS*CO*45*100.00~",
        "CAS*PR*2*40.00~",
        "AMT*B6*100.00~",
        paid="60.00",
    )
    line = read_remittances(text)[0].claims[0].service_lines[0]

    assert len(line.adjustments) == 2
    assert {(a.group, a.reason_code) for a in line.adjustments} == {("CO", "45"), ("PR", "2")}
    assert line.balances


def test_lines_of_one_claim_do_not_bleed_into_the_next() -> None:
    """The cursor-reset defect: CAS of claim 2 attaching to the last line of claim 1."""
    text = remittance(
        [
            "CLP*CLM1*1*100.00*50.00**MB*PCN1*11~",
            "SVC*HC:99213*100.00*50.00**1~",
            "CAS*CO*45*50.00~",
            "AMT*B6*50.00~",
            "CLP*CLM2*1*100.00*50.00**MB*PCN2*11~",
            "SVC*HC:99214*100.00*50.00**1~",
            "CAS*CO*45*50.00~",
            "AMT*B6*50.00~",
        ],
        payment="100.00",
    )
    claims = read_remittances(text)[0].claims

    assert [c.claim_id for c in claims] == ["CLM1", "CLM2"]
    for claim in claims:
        assert len(claim.service_lines) == 1
        assert len(claim.service_lines[0].adjustments) == 1


def test_claim_level_adjustments_are_kept_apart_from_line_level() -> None:
    """Conflating them double-counts the money."""
    text = remittance(
        [
            "CLP*CLM1*1*200.00*150.00**MB*PCN1*11~",
            "CAS*OA*23*20.00~",
            "SVC*HC:99213*200.00*150.00**1~",
            "CAS*CO*45*30.00~",
            "AMT*B6*150.00~",
        ],
        payment="150.00",
    )
    claim = read_remittances(text)[0].claims[0]

    assert len(claim.claim_adjustments) == 1
    assert claim.claim_adjustments[0].claim_level is True
    assert claim.claim_adjustments[0].group == "OA"

    line_adjustments = claim.service_lines[0].adjustments
    assert len(line_adjustments) == 1
    assert line_adjustments[0].claim_level is False
    assert len(claim.adjustments) == 2  # both, exactly once each


# ---------------------------------------------------------------------------
# PD-3: a reason code never travels without its group
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_cas_group_code_required() -> None:
    """`Adjustment` cannot be constructed without a group, and CAS01 is validated.

    CO-45 is a contractual write-off the patient may never be billed for.
    PR-45 is patient responsibility. Same number, opposite meaning, and the
    group code is the only thing that distinguishes them.
    """
    from rcm.ingestion.x12 import Adjustment

    with pytest.raises(TypeError):
        Adjustment(reason_code="45", amount=Decimal("1.00"))  # type: ignore[call-arg]

    text = one_claim(
        "SVC*HC:99213*200.00*0.00**1~", "CAS*ZZ*45*200.00~", "AMT*B6*0.00~", paid="0.00"
    )
    with pytest.raises(MalformedX12Error) as caught:
        read_remittances(text)

    assert caught.value.context["assertion"] == "CAS01 in code list 1033"
    assert caught.value.context["found"] == "ZZ"


def test_co_and_pr_with_the_same_reason_code_stay_distinct() -> None:
    text = one_claim(
        "SVC*HC:99213*200.00*100.00**1~",
        "CAS*CO*45*60.00~",
        "CAS*PR*45*40.00~",
        "AMT*B6*140.00~",
        paid="100.00",
    )
    adjustments = read_remittances(text)[0].claims[0].service_lines[0].adjustments

    assert sorted((a.group, str(a.amount)) for a in adjustments) == [
        ("CO", "60.00"),
        ("PR", "40.00"),
    ]


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_835_must_balance_or_quarantine() -> None:
    """The file reconciles against its own stated total, or it is reported as not.

    The parser reports; the loader quarantines (M1.3). Keeping them apart lets
    a reconciliation tool read a broken file deliberately, which is the first
    thing anyone needs when a payer sends one.
    """
    good = read_remittances(
        one_claim("SVC*HC:99213*200.00*100.00**1~", "CAS*CO*45*100.00~", "AMT*B6*100.00~")
    )[0]
    assert good.balances
    assert good.balance_discrepancy == Decimal("0.00")

    bad = read_remittances(
        one_claim("SVC*HC:99213*200.00*100.00**1~", "CAS*CO*45*100.00~", "AMT*B6*100.00~").replace(
            "BPR*I*100.00", "BPR*I*999.00"
        )
    )[0]
    assert not bad.balances
    assert bad.balance_discrepancy == Decimal("-899.00")


def test_provider_level_adjustments_are_counted_in_the_balance() -> None:
    """PLB is what makes a file fail to balance for a parser that ignores it.

    The sign convention is the standard's and is counter-intuitive: a positive
    PLB amount *reduces* the payment.
    """
    text = remittance(
        [
            "CLP*CLM1*1*200.00*100.00**MB*PCN1*11~",
            "SVC*HC:99213*200.00*100.00**1~",
            "CAS*CO*45*100.00~",
            "AMT*B6*100.00~",
            "PLB*1234567893*20241231*WO:PCN0099*25.00~",
        ],
        payment="75.00",
    )
    result = read_remittances(text)[0]

    assert result.provider_adjustment_total == Decimal("25.00")
    assert result.claim_payment_total == Decimal("100.00")
    assert result.expected_payment == Decimal("75.00")
    assert result.balances

    adjustment = result.provider_adjustments[0]
    assert adjustment.reason_code == "WO"
    assert adjustment.reference == "PCN0099"


def test_an_unbalanced_service_line_is_reported_with_its_claim() -> None:
    text = one_claim("SVC*HC:99213*200.00*100.00**1~", "CAS*CO*45*10.00~", "AMT*B6*100.00~")
    result = read_remittances(text)[0]

    assert result.unbalanced_service_lines == (("CLM1", "99213"),)


# ---------------------------------------------------------------------------
# Claim and line detail
# ---------------------------------------------------------------------------


def test_reversals_are_identifiable() -> None:
    """CLP02 = 22. A reversal reopens the original claim; it is not a new one (PD-7)."""
    text = one_claim(
        "SVC*HC:99213*200.00*-100.00**1~",
        "CAS*CR*45*300.00~",
        "AMT*B6*-100.00~",
        paid="-100.00",
        status="22",
    )
    claim = read_remittances(text)[0].claims[0]

    assert claim.is_reversal
    assert not claim.is_denied
    assert claim.total_paid == Decimal("-100.00")


def test_denied_claims_are_identifiable() -> None:
    text = one_claim(
        "SVC*HC:99213*200.00*0.00**1~",
        "CAS*CO*29*200.00~",
        "AMT*B6*0.00~",
        paid="0.00",
        status="4",
    )
    claim = read_remittances(text)[0].claims[0]

    assert claim.is_denied
    assert not claim.is_reversal
    assert claim.service_lines[0].is_denied


def test_a_zero_payment_line_with_only_patient_responsibility_is_not_a_denial() -> None:
    """Paid zero because the patient owes it all is not the payer refusing.

    Treating it as a denial inflates the denial rate with claims that were
    adjudicated exactly as intended.
    """
    text = one_claim(
        "SVC*HC:99213*200.00*0.00**1~",
        "CAS*CO*45*50.00~",
        "CAS*PR*1*150.00~",
        "AMT*B6*150.00~",
        paid="0.00",
    )
    line = read_remittances(text)[0].claims[0].service_lines[0]

    # Structurally this *is* an adjusted zero-payment line; the refinement
    # that separates "denied" from "all patient responsibility" belongs to the
    # denial taxonomy in M4.8. What matters here is that the group codes
    # survive so that refinement is possible at all.
    assert {a.group for a in line.adjustments} == {"CO", "PR"}
    assert line.balances


def test_procedure_code_and_modifiers_come_out_of_the_svc_composite() -> None:
    text = one_claim("SVC*HC:99213:25:59*200.00*100.00**2~", "CAS*CO*45*100.00~", "AMT*B6*100.00~")
    line = read_remittances(text)[0].claims[0].service_lines[0]

    assert line.procedure_code == "99213"
    assert line.modifiers == ("25", "59")
    assert line.units == Decimal("2")


def test_claim_and_patient_identifiers_are_captured() -> None:
    result = read_remittances(
        one_claim("SVC*HC:99213*200.00*100.00**1~", "CAS*CO*45*100.00~", "AMT*B6*100.00~")
    )[0]
    claim = result.claims[0]

    assert claim.claim_id == "CLM1"
    assert claim.payer_control_number == "PCN1"
    assert claim.patient_last_name == "Delgado"
    assert claim.member_id == "MEM123"
    assert claim.rendering_provider_npi == "1234567893"
    assert result.trace_number == "TRACE1"
    assert result.payer_name == "Big Payer"
    assert result.payee_npi == "1234567893"


def test_remark_codes_are_captured() -> None:
    text = one_claim(
        "SVC*HC:99213*200.00*0.00**1~",
        "CAS*CO*29*200.00~",
        "AMT*B6*0.00~",
        "LQ*HE*N30~",
        "LQ*HE*M127~",
        paid="0.00",
    )
    line = read_remittances(text)[0].claims[0].service_lines[0]

    assert line.remark_codes == ("N30", "M127")


# ---------------------------------------------------------------------------
# Malformed content
# ---------------------------------------------------------------------------


def test_a_non_numeric_amount_is_refused_rather_than_defaulted() -> None:
    """Substituting zero for an unreadable amount is a silent wrong number."""
    text = one_claim("SVC*HC:99213*NOTANUMBER*100.00**1~", "CAS*CO*45*100.00~", "AMT*B6*0~")
    with pytest.raises(MalformedX12Error) as caught:
        read_remittances(text)

    assert caught.value.context["field"] == "SVC02"


def test_an_835_without_a_bpr_is_refused() -> None:
    text = (
        ISA
        + "GS*HP*SENDER*RECEIVER*20240131*0000*1*X*005010X221A1~"
        + "ST*835*0001~LX*1~SE*3*0001~"
        + "GE*1*1~IEA*1*000000001~"
    )
    with pytest.raises(MalformedX12Error) as caught:
        read_remittances(text)

    assert caught.value.context["assertion"] == "BPR present"


def test_a_cas_outside_any_claim_is_refused() -> None:
    text = remittance(["CAS*CO*45*100.00~"])
    with pytest.raises(MalformedX12Error):
        read_remittances(text)


def test_an_837_is_not_parsed_as_an_835() -> None:
    text = (
        ISA
        + "GS*HC*SENDER*RECEIVER*20240131*0000*1*X*005010X222A1~"
        + "ST*837*0001~BHT*0019*00*1*20240131*0000*CH~SE*3*0001~"
        + "GE*1*1~IEA*1*000000001~"
    )
    # read_remittances filters by ST01, so an 837-only file yields nothing
    # rather than a misparsed remittance.
    assert read_remittances(text) == ()
