"""The acknowledgement family: 999, 277CA and 271 (M1.6).

Three parsers that answer three different questions about the same claim, and
the tests here are mostly about keeping the answers apart:

    999    did the transmission parse?      syntax only
    277CA  did the payer accept the claim?  before adjudication
    271    is the patient covered?          before the claim exists

The ones that matter most assert that a *rejection* is recognised as a
rejection. A parser that reads `A3` as acceptance, or `R` as `A`, reports
success for a claim that is sitting at a clearinghouse going nowhere, and the
first anyone hears of it is a timely-filing write-off months later.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from rcm.ingestion.x12 import read_acknowledgements, read_claim_statuses, read_eligibility
from rcm.platform.errors import MalformedX12Error, PlatformError


def isa(sender: str = "CLEARINGHSE1", receiver: str = "SUBMITTER01") -> str:
    return (
        "ISA*00*"
        + " " * 10
        + "*00*"
        + " " * 10
        + "*ZZ*"
        + sender.ljust(15)
        + "*ZZ*"
        + receiver.ljust(15)
        + "*240131*0000*^*00501*000000001*0*T*:~"
    )


def envelope(body: list[str], *, functional: str, version: str, control: str = "0001") -> bytes:
    segments = [*body]
    segments.append(f"SE*{len(segments) + 1}*{control}~")
    return (
        isa()
        + f"GS*{functional}*CLEARINGHSE1*SUBMITTER01*20240131*0000*1*X*{version}~"
        + "".join(segments)
        + "GE*1*1~IEA*1*000000001~"
    ).encode()


# ---------------------------------------------------------------------------
# 999 — did the transmission parse
# ---------------------------------------------------------------------------


def ack_999(*, status: str = "A", detail: list[str] | None = None, control: str = "0001") -> bytes:
    body = [
        f"ST*999*{control}*005010X231A1~",
        "AK1*HC*100072*005010X222A1~",
        *(detail or []),
        f"AK9*{status}*1*1*{'1' if status == 'A' else '0'}~",
    ]
    return envelope(body, functional="FA", version="005010X231A1", control=control)


def test_an_accepted_999_is_accepted() -> None:
    ack = read_acknowledgements(ack_999())[0]
    assert ack.accepted is True
    assert ack.rejected is False
    assert ack.functional_identifier == "HC"
    assert ack.group_control_number == "100072"
    assert ack.counts_reconcile is True


@pytest.mark.parametrize("status", ["R", "M", "W", "X"])
def test_a_rejected_999_is_rejected(status: str) -> None:
    ack = read_acknowledgements(ack_999(status=status))[0]
    assert ack.rejected is True
    assert ack.accepted is False


def test_accepted_with_errors_is_still_accepted() -> None:
    """`E` means the transmission went through with notes.

    Treating it as a rejection stops a submission that actually landed.
    """
    assert read_acknowledgements(ack_999(status="E"))[0].accepted is True


@pytest.mark.invariant
def test_partially_accepted_is_not_acceptance() -> None:
    """`P` means at least one transaction set was rejected.

    A pipeline that reads it as acceptance loses exactly the claims that
    failed, and loses them silently (PD-18: fail closed).
    """
    ack = read_acknowledgements(ack_999(status="P"))[0]
    assert ack.accepted is False
    assert ack.rejected is True


def test_the_error_detail_names_the_segment_and_the_element() -> None:
    """A 999 saying only "rejected" sends an operator to open the file by hand."""
    ack = read_acknowledgements(
        ack_999(
            status="R",
            detail=[
                "AK2*837*0001*005010X222A1~",
                "IK3*CLM*23*2300*8~",
                "IK4*2*782*7*NOTANUMBER~",
                "IK5*R*5~",
            ],
        )
    )[0]

    assert len(ack.rejected_transactions) == 1
    transaction = ack.transactions[0]
    assert transaction.transaction_set_identifier == "837"
    assert transaction.control_number == "0001"
    assert transaction.rejected is True

    error = transaction.errors[0]
    assert error.segment_id == "CLM"
    assert error.segment_position == 23
    assert error.loop_identifier == "2300"
    assert error.element_position == 2
    assert error.bad_value == "NOTANUMBER"


def test_several_elements_in_one_segment_are_each_recorded() -> None:
    """Two IK4s under one IK3 are two findings, not one overwritten."""
    ack = read_acknowledgements(
        ack_999(
            status="R",
            detail=[
                "AK2*837*0001~",
                "IK3*CLM*23*2300*8~",
                "IK4*2*782*7*NOTANUMBER~",
                "IK4*5*1331*7*BADPOS~",
                "IK5*R*5~",
            ],
        )
    )[0]
    positions = [e.element_position for e in ack.transactions[0].errors]
    assert positions == [2, 5]


def test_a_segment_error_with_no_element_detail_is_kept() -> None:
    """IK3 without IK4 still names the segment, which is actionable."""
    ack = read_acknowledgements(
        ack_999(status="R", detail=["AK2*837*0001~", "IK3*CLM*23*2300*8~", "IK5*R~"])
    )[0]
    error = ack.transactions[0].errors[0]
    assert error.segment_id == "CLM"
    assert error.element_position is None


def test_a_999_without_a_trailer_is_refused() -> None:
    """No AK9 means the file states no verdict, which is not a verdict of
    acceptance."""
    body = ["ST*999*0001*005010X231A1~", "AK1*HC*100072*005010X222A1~"]
    with pytest.raises(MalformedX12Error):
        read_acknowledgements(envelope(body, functional="FA", version="005010X231A1"))


def test_counts_that_disagree_are_reported_not_raised() -> None:
    body = [
        "ST*999*0001*005010X231A1~",
        "AK1*HC*100072*005010X222A1~",
        "AK9*A*5*3*3~",
    ]
    ack = read_acknowledgements(envelope(body, functional="FA", version="005010X231A1"))[0]
    assert ack.counts_reconcile is False
    assert ack.accepted is True


# ---------------------------------------------------------------------------
# 277CA — did the payer accept the claim
# ---------------------------------------------------------------------------


def ack_277(*statuses: tuple[str, str], control: str = "0001") -> bytes:
    """`statuses` is (claim_id, "A2:20"-style composite)."""
    body = [
        f"ST*277*{control}*005010X214~",
        f"BHT*0085*08*{control}*20240131*0000*TH~",
        "HL*1**20*1~",
        "NM1*PR*2*BCBS Texas*****PI*00590~",
        "TRN*1*BATCH1~",
        "HL*2*1*21*1~",
        "NM1*41*2*RCM SEED BILLING*****46*SUBMITTER01~",
        "HL*3*2*19*1~",
        "NM1*85*2*Lakeview Clinic*****XX*4952654158~",
    ]
    level = 4
    for claim_id, composite in statuses:
        body.extend(
            [
                f"HL*{level}*3*22*0~",
                "NM1*QC*1*Castellanos*Andre~",
                f"TRN*2*{claim_id}~",
                f"STC*{composite}*20240131*WQ*200.00********Missing subscriber id~",
                f"REF*1K*PCN{claim_id}~",
                "DTP*472*D8*20240101~",
            ]
        )
        level += 1
    return envelope(body, functional="HN", version="005010X214", control=control)


@pytest.mark.parametrize("category", ["A1", "A2"])
def test_an_accepted_claim_status_is_accepted(category: str) -> None:
    ack = read_claim_statuses(ack_277(("CLM1", f"{category}:20")))[0]
    status = ack.statuses[0]
    assert status.accepted is True
    assert status.rejected is False
    assert ack.accepted_claims == (status,)


@pytest.mark.invariant
@pytest.mark.parametrize("category", ["A3", "A4", "A6", "A7", "A8"])
def test_a_rejected_claim_status_is_rejected(category: str) -> None:
    """PD-4. Every one of these means the claim was returned unadjudicated.

    A parser that reads any of them as acceptance reports success for a claim
    that is going nowhere, and the first anyone hears of it is a
    timely-filing write-off months later.
    """
    ack = read_claim_statuses(ack_277(("CLM1", f"{category}:21")))[0]
    status = ack.statuses[0]
    assert status.rejected is True
    assert status.accepted is False
    assert ack.rejected_claims == (status,)


def test_the_category_decides_not_the_status_code() -> None:
    """The same detailed code means accepted or rejected depending on the
    category it arrives under, so reading STC01-2 alone gives a number with no
    verdict attached."""
    accepted = read_claim_statuses(ack_277(("CLM1", "A2:20")))[0].statuses[0]
    rejected = read_claim_statuses(ack_277(("CLM1", "A3:20")))[0].statuses[0]

    assert accepted.status_code == rejected.status_code == "20"
    assert accepted.accepted and rejected.rejected


def test_the_claim_status_carries_its_evidence() -> None:
    ack = read_claim_statuses(ack_277(("CLM1", "A3:21")))[0]
    status = ack.statuses[0]
    assert status.claim_id == "CLM1"
    assert status.payer_claim_number == "PCNCLM1"
    assert status.effective_date == date(2024, 1, 31)
    assert status.total_charge == Decimal("200.00")
    assert status.description == "Missing subscriber id"
    assert status.patient_last_name == "Castellanos"


def test_several_claims_in_one_acknowledgement_are_kept_apart() -> None:
    ack = read_claim_statuses(ack_277(("CLM1", "A2:20"), ("CLM2", "A3:21"), ("CLM3", "A2:20")))[0]
    assert [s.claim_id for s in ack.statuses] == ["CLM1", "CLM2", "CLM3"]
    assert [s.claim_id for s in ack.rejected_claims] == ["CLM2"]
    assert len(ack.accepted_claims) == 2


@pytest.mark.invariant
def test_an_unknown_category_is_neither_accepted_nor_rejected() -> None:
    """PD-18. A category outside both lists has no verdict this platform can
    read, and inventing one means a claim silently acquires an outcome nobody
    chose. It is surfaced so the loader can refuse the file."""
    ack = read_claim_statuses(ack_277(("CLM1", "ZZ:99")))[0]
    status = ack.statuses[0]

    assert status.accepted is False
    assert status.rejected is False
    assert status.is_known_category is False
    assert ack.unknown_categories == ("ZZ",)


def test_a_status_outside_any_claim_loop_is_refused() -> None:
    """An STC naming no claim cannot be attributed to one."""
    body = [
        "ST*277*0001*005010X214~",
        "BHT*0085*08*0001*20240131*0000*TH~",
        "STC*A2:20*20240131*WQ*200.00~",
    ]
    with pytest.raises(MalformedX12Error):
        read_claim_statuses(envelope(body, functional="HN", version="005010X214"))


def test_an_empty_status_composite_is_refused() -> None:
    body = [
        "ST*277*0001*005010X214~",
        "BHT*0085*08*0001*20240131*0000*TH~",
        "HL*4*3*22*0~",
        "TRN*2*CLM1~",
        "STC**20240131*WQ~",
    ]
    with pytest.raises(MalformedX12Error):
        read_claim_statuses(envelope(body, functional="HN", version="005010X214"))


# ---------------------------------------------------------------------------
# 271 — is the patient covered
# ---------------------------------------------------------------------------


def response_271(*benefits: str, problems: list[str] | None = None) -> bytes:
    body = [
        "ST*271*0001*005010X279A1~",
        "BHT*0022*11*0001*20240131*0000~",
        "HL*1**20*1~",
        "NM1*PR*2*BCBS Texas*****PI*00590~",
        "HL*2*1*21*1~",
        "NM1*1P*2*Lakeview Clinic*****XX*4952654158~",
        "HL*3*2*22*0~",
        "NM1*IL*1*Castellanos*Andre****MI*VAZ957419823~",
        "DMG*D8*19640111*F~",
        *benefits,
        *(problems or []),
    ]
    return envelope(body, functional="HB", version="005010X279A1")


def test_active_coverage_is_read() -> None:
    response = read_eligibility(response_271("EB*1*IND*30**Gold PPO~"))[0]
    assert response.answered is True
    assert response.is_active is True
    assert response.member_id == "VAZ957419823"
    assert response.date_of_birth == date(1964, 1, 11)


@pytest.mark.parametrize("code", ["6", "7", "8", "I"])
def test_inactive_coverage_is_read(code: str) -> None:
    response = read_eligibility(response_271(f"EB*{code}*IND*30~"))[0]
    assert response.answered is True
    assert response.is_active is False


@pytest.mark.invariant
def test_active_pending_investigation_still_pays() -> None:
    """`5` is active-pending-investigation and `7` is inactive-pending-update.

    They read almost identically and mean opposite things. Collapsing them
    denies coverage to a patient who has it.
    """
    assert read_eligibility(response_271("EB*5*IND*30~"))[0].is_active is True
    assert read_eligibility(response_271("EB*7*IND*30~"))[0].is_active is False


def test_a_copay_benefit_states_no_coverage_either_way() -> None:
    """Most EB segments describe a copay or a limitation rather than
    eligibility, and treating a copay amount as proof of coverage is how an
    eligibility check becomes a rubber stamp."""
    response = read_eligibility(response_271("EB*B*IND*30***27*25.00~"))[0]
    benefit = response.benefits[0]

    assert benefit.states_coverage is False
    assert benefit.amount == Decimal("25.00")
    assert response.answered is False


@pytest.mark.invariant
def test_a_refused_enquiry_is_not_a_negative_answer() -> None:
    """PD-18. "We could not look" and "they are not covered" lead to opposite
    actions, and the second one bills an insured patient."""
    response = read_eligibility(response_271(problems=["AAA*Y**72*C~"]))[0]

    assert response.answered is False
    assert response.is_active is False
    assert len(response.problems) == 1
    assert response.problems[0].reject_reason_code == "72"
    assert response.problems[0].valid_request is True


def test_coverage_is_a_period_not_a_state() -> None:
    """A 271 answering about today says nothing about a date of service three
    weeks ago."""
    response = read_eligibility(response_271("EB*1*IND*30~", "DTP*291*RD8*20240101-20240331~"))[0]

    assert response.active_on(date(2024, 2, 15)) is True
    assert response.active_on(date(2023, 12, 31)) is False
    assert response.active_on(date(2024, 4, 1)) is False


def test_an_open_ended_period_is_still_in_force() -> None:
    response = read_eligibility(response_271("EB*1*IND*30~", "DTP*291*D8*20240101~"))[0]
    assert response.active_on(date(2030, 1, 1)) is True
    assert response.active_on(date(2023, 1, 1)) is False


def test_a_benefit_with_no_period_does_not_exclude_any_date() -> None:
    """The payer did not say, and inventing a window would deny coverage the
    response never denied."""
    response = read_eligibility(response_271("EB*1*IND*30~"))[0]
    assert response.active_on(date(2024, 2, 15)) is True


def test_an_unreadable_period_does_not_make_the_response_unreadable() -> None:
    """A malformed date inside a coverage period costs that period, not the
    benefits that parsed."""
    response = read_eligibility(response_271("EB*1*IND*30~", "DTP*291*RD8*NOTADATE-20240331~"))[0]
    assert response.is_active is True
    assert response.benefits[0].periods[0].start is None


# ---------------------------------------------------------------------------
# Every refusal is named
# ---------------------------------------------------------------------------


def test_every_refusal_is_a_platform_error() -> None:
    """DR-10: a quarantined file must name the assertion it failed, and an
    unnamed exception names nothing."""
    for broken in (
        b"",
        b"ISA*not an interchange~",
        envelope(["ST*999*0001~"], functional="FA", version="005010X231A1"),
    ):
        with pytest.raises(PlatformError):
            read_acknowledgements(broken)
