"""Reversals and recoupments: taking money back (M1.10).

    BR-M9-04 (MUST): Reversals (claim status 22) MUST reverse the original
    posting and re-open the claim, not create a new negative claim.

Two different events, deliberately tested apart:

    reversal     the payer re-adjudicates a claim it already paid and sends a
                 later remittance negating the original posting
    recoupment   the payer keeps the adjudication and reduces a *later* cheque
                 to recover the money (EC-47)

Both reopen the claim; only one changes its outcome. Conflating them loses the
information that tells an operator which has happened.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text

from rcm.analytics.denials import denial_rate
from rcm.analytics.reversals import reopened_claim_ids, reopened_claims
from rcm.domain.claim import Claim
from rcm.domain.remittance import ClaimPayment, ProviderAdjustment
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.remittance_loader import LoadOutcome, load_remittance_file
from rcm.platform.db import reset_engines, session_scope
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.integration

CLIENT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
CLIENT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")
CHARGE = Decimal("200.00")
PAID = Decimal("150.00")
WRITE_OFF = CHARGE - PAID


def isa(sender: str = "PAYER0001", receiver: str = "PAYEE0001") -> str:
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


def _envelope(body: list[str], control: str, check: Decimal, plbs: list[str]) -> bytes:
    header = [
        f"ST*835*{control}~",
        f"BPR*I*{check}*C*ACH*CCP*01*999999999*DA*1234*****20240131~",
        f"TRN*1*TRACE{control}*9000~",
        "REF*EV*PAYER0001~",
        "DTM*405*20240131~",
        "N1*PR*BCBS Texas~",
        "N1*PE*A Practice*XX*1234567893~",
        "LX*1~",
        *body,
        *plbs,
    ]
    header.append(f"SE*{len(header) + 1}*{control}~")
    return (
        isa()
        + "GS*HP*PAYER0001*PAYEE0001*20240131*0000*1*X*005010X221A1~"
        + "".join(header)
        + "GE*1*1~IEA*1*000000001~"
    ).encode()


def payment_for(claim_id: str, *, control: str = "0001", pcn: str | None = None) -> bytes:
    """An ordinary 835 paying one claim: charged 200, paid 150, 50 written off."""
    reference = pcn or f"PCN{claim_id}"
    body = [
        f"CLP*{claim_id}*1*{CHARGE}*{PAID}**MB*{reference}*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*{PAID}**1~",
        f"CAS*CO*45*{WRITE_OFF}~",
        f"AMT*B6*{PAID}~",
    ]
    return _envelope(body, control, PAID, [])


def reversal_for(claim_id: str, *, control: str = "0002", pcn: str | None = None) -> bytes:
    """The same payment, months later, with every sign flipped.

    That mirroring is what keeps the file balanced: `-charge == -paid +
    -adjustments` holds exactly when the original held. CLP02 is 22.
    """
    reference = pcn or f"PCN{claim_id}"
    body = [
        f"CLP*{claim_id}*22*{-CHARGE}*{-PAID}**MB*{reference}*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{-CHARGE}*{-PAID}**1~",
        f"CAS*CO*45*{-WRITE_OFF}~",
        f"AMT*B6*{-PAID}~",
    ]
    return _envelope(body, control, -PAID, [])


def recoupment_for(
    recouped_claim: str, *, paid_claim: str, control: str = "0003", pcn: str | None = None
) -> bytes:
    """A later cheque for one claim, reduced to claw back an earlier one.

    The PLB reduces the payment, so the file balances at `paid - clawed back`.
    A positive PLB amount *reduces* the cheque: that is the standard's sign
    convention and it reads backwards.
    """
    reference = pcn or f"PCN{recouped_claim}"
    body = [
        f"CLP*{paid_claim}*1*{CHARGE}*{PAID}**MB*PCN{paid_claim}*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*{PAID}**1~",
        f"CAS*CO*45*{WRITE_OFF}~",
        f"AMT*B6*{PAID}~",
    ]
    plbs = [f"PLB*1234567893*20241231*WO:{reference}*{PAID}~"]
    return _envelope(body, control, PAID - PAID, plbs)


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def clean_core(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE claim_line, claim, remittance_adjustment, "
                    "remittance_service_line, claim_payment, provider_adjustment, "
                    "remittance, raw_file, payer_alias, payer RESTART IDENTITY CASCADE"
                )
            )

    reset_engines()
    wipe()
    yield
    wipe()


def submit(claim_id: str, root: Path, *, client: uuid.UUID = CLIENT_A, control: str) -> None:
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim(claim_id, charge=str(CHARGE)), control=control),
            client_id=client,
            original_filename=f"{claim_id}.edi",
            archive_root=root,
        )


def remit(payload: bytes, root: Path, *, client: uuid.UUID = CLIENT_A, name: str):
    with session_scope() as session:
        return load_remittance_file(
            session, payload, client_id=client, original_filename=name, archive_root=root
        )


# ---------------------------------------------------------------------------
# BR-M9-04
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_reversal_reopens_original_claim(archive_root: Path) -> None:
    """A paid claim reversed months later reopens and creates no new claim.

    The acceptance condition for M1.10, stated in CLAUDE.md as "paid claim
    reversed months later reopens, does not create negative claim".
    """
    submit("CLM1", archive_root, control="000100001")
    remit(payment_for("CLM1"), archive_root, name="pay.edi")

    with session_scope() as session:
        before = session.scalar(select(func.count()).select_from(Claim))

    result = remit(reversal_for("CLM1"), archive_root, name="rev.edi")
    assert result.outcome is LoadOutcome.LOADED

    with session_scope() as session:
        # No new claim, and certainly no negative one.
        assert session.scalar(select(func.count()).select_from(Claim)) == before
        assert (
            session.scalar(select(func.count()).select_from(Claim).where(Claim.total_charge < 0))
            == 0
        )

        postings = session.scalars(select(ClaimPayment).order_by(ClaimPayment.created_at)).all()
        assert len(postings) == 2
        original, reversal = postings
        assert original.is_reversal is False
        assert reversal.is_reversal is True
        # The reversal points at what it reverses.
        assert reversal.reverses_claim_payment_id == original.claim_payment_id
        assert reversal.total_paid == -PAID

        # And the claim is reopened.
        assert reopened_claims(session, client_id=CLIENT_A).reversed_claims == 1
        assert len(reopened_claim_ids(session, client_id=CLIENT_A)) == 1


def test_a_reversal_is_matched_on_the_payer_control_number(archive_root: Path) -> None:
    """CLP07, not the submitted claim id.

    DOC-02 section 6: *"The payer's claim number (CLP07) is how you match
    resubmissions and reversals. Store it."* It is the only handle that
    survives a resubmission, where the submitter's number changes and the
    payer's does not.
    """
    submit("CLM1", archive_root, control="000100001")
    remit(payment_for("CLM1", pcn="PAYER-REF-9"), archive_root, name="pay.edi")
    # The *resubmitted* claim id, which is what makes this test able to fail:
    # with the same id on both postings a loader matching on CLM01 would pass
    # too, and the first version of this test did exactly that.
    remit(reversal_for("CLM1R1", pcn="PAYER-REF-9"), archive_root, name="rev.edi")

    with session_scope() as session:
        reversal = session.scalar(select(ClaimPayment).where(ClaimPayment.is_reversal.is_(True)))
        original = session.scalar(select(ClaimPayment).where(ClaimPayment.is_reversal.is_(False)))
        assert reversal is not None
        assert original is not None
        assert reversal.reverses_claim_payment_id == original.claim_payment_id


def test_a_second_reversal_links_to_the_payment_not_the_first_reversal(
    archive_root: Path,
) -> None:
    """Otherwise the chain becomes a history and the pairing is lost.

    A reversal reverses a *payment*. Linking the second one to the first would
    read as "this reverses that reversal", which is a re-payment, and is the
    opposite of what happened.
    """
    submit("CLM1", archive_root, control="000100001")
    remit(payment_for("CLM1"), archive_root, name="pay.edi")
    remit(reversal_for("CLM1", control="0002"), archive_root, name="rev1.edi")
    remit(reversal_for("CLM1", control="0009"), archive_root, name="rev2.edi")

    with session_scope() as session:
        original = session.scalar(select(ClaimPayment).where(ClaimPayment.is_reversal.is_(False)))
        reversals = session.scalars(
            select(ClaimPayment).where(ClaimPayment.is_reversal.is_(True))
        ).all()
        assert original is not None
        assert len(reversals) == 2
        assert {r.reverses_claim_payment_id for r in reversals} == {original.claim_payment_id}

        # Still one reopened claim, not two.
        assert reopened_claims(session, client_id=CLIENT_A).reversed_claims == 1


def test_reversals_do_not_link_to_each_other_when_the_payment_is_missing(
    archive_root: Path,
) -> None:
    """The case that makes excluding reversals from the search load-bearing.

    While the original payment is present it is also the earliest posting, so
    ordering alone picks it and the exclusion looks redundant. It stops being
    redundant the moment the payment is absent: without it, the second
    reversal links to the first and the records read as "this reversal was
    itself reversed", which is a re-payment and the opposite of what happened.
    """
    submit("CLM1", archive_root, control="000100001")
    remit(reversal_for("CLM1", control="0002"), archive_root, name="rev1.edi")
    remit(reversal_for("CLM1", control="0009"), archive_root, name="rev2.edi")

    with session_scope() as session:
        reversals = session.scalars(select(ClaimPayment).order_by(ClaimPayment.created_at)).all()
        assert len(reversals) == 2
        assert all(r.is_reversal for r in reversals)
        assert [r.reverses_claim_payment_id for r in reversals] == [None, None]


def test_a_reversal_for_an_unknown_claim_is_still_recorded(archive_root: Path) -> None:
    """The money moved whether or not this platform has the other half.

    Refusing the file would lose the record of a payment being taken back,
    which is worse than holding a reversal whose original is missing.
    """
    result = remit(reversal_for("CLM-NEVER-SEEN"), archive_root, name="rev.edi")
    assert result.outcome is LoadOutcome.LOADED

    with session_scope() as session:
        reversal = session.scalar(select(ClaimPayment))
        assert reversal is not None
        assert reversal.is_reversal is True
        assert reversal.reverses_claim_payment_id is None


def test_a_reversal_does_not_chain_across_clients(archive_root: Path) -> None:
    """PD-20: client A's payment is not the original for client B's reversal."""
    submit("CLM1", archive_root, client=CLIENT_A, control="000100001")
    remit(payment_for("CLM1"), archive_root, client=CLIENT_A, name="pay.edi")
    remit(reversal_for("CLM1"), archive_root, client=CLIENT_B, name="rev.edi")

    with session_scope() as session:
        reversal = session.scalar(select(ClaimPayment).where(ClaimPayment.client_id == CLIENT_B))
        assert reversal is not None
        assert reversal.is_reversal is True
        assert reversal.reverses_claim_payment_id is None


# ---------------------------------------------------------------------------
# EC-47: recoupment
# ---------------------------------------------------------------------------


def test_a_recoupment_links_to_the_payment_it_claws_back(archive_root: Path) -> None:
    """The payer reduces today's cheque to recover an earlier overpayment."""
    submit("CLM1", archive_root, control="000100001")
    submit("CLM2", archive_root, control="000100002")
    remit(payment_for("CLM1"), archive_root, name="pay1.edi")
    remit(recoupment_for("CLM1", paid_claim="CLM2"), archive_root, name="recoup.edi")

    with session_scope() as session:
        recoupment = session.scalar(select(ProviderAdjustment))
        original = session.scalar(select(ClaimPayment).where(ClaimPayment.claim_id == "CLM1"))
        assert recoupment is not None
        assert original is not None
        assert recoupment.reason_code == "WO"
        assert recoupment.recoups_claim_payment_id == original.claim_payment_id

        summary = reopened_claims(session, client_id=CLIENT_A)
        assert summary.recouped_claims == 1
        assert summary.reversed_claims == 0


def test_a_recoupment_creates_no_claim_and_no_negative_claim(archive_root: Path) -> None:
    submit("CLM1", archive_root, control="000100001")
    submit("CLM2", archive_root, control="000100002")
    remit(payment_for("CLM1"), archive_root, name="pay1.edi")

    with session_scope() as session:
        before = session.scalar(select(func.count()).select_from(Claim))

    remit(recoupment_for("CLM1", paid_claim="CLM2"), archive_root, name="recoup.edi")

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Claim)) == before
        assert (
            session.scalar(select(func.count()).select_from(Claim).where(Claim.total_charge < 0))
            == 0
        )


def test_an_interest_payment_is_not_a_recoupment(archive_root: Path) -> None:
    """Only `WO` entries claw back a specific claim.

    Interest and forwarding balances move money without undoing a payment, and
    linking them would attribute a claw-back to a claim that was never
    over-paid.
    """
    submit("CLM1", archive_root, control="000100001")
    remit(payment_for("CLM1"), archive_root, name="pay.edi")

    body = [
        f"CLP*CLM1*1*{CHARGE}*{PAID}**MB*PCNCLM1*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*{PAID}**1~",
        f"CAS*CO*45*{WRITE_OFF}~",
        f"AMT*B6*{PAID}~",
    ]
    # L6 is interest, not a claw-back. It deliberately *does* name a claim in
    # PLB03-2, so the only thing that can tell it apart from a recoupment is
    # the reason code. An earlier version omitted the reference, which meant
    # the test passed because the reference was empty rather than because the
    # reason was wrong.
    interest = _envelope(
        body, "0004", PAID - Decimal("5.00"), ["PLB*1234567893*20241231*L6:PCNCLM1*5.00~"]
    )
    remit(interest, archive_root, name="interest.edi")

    with session_scope() as session:
        entry = session.scalar(select(ProviderAdjustment))
        assert entry is not None
        assert entry.reason_code == "L6"
        assert entry.recoups_claim_payment_id is None
        assert reopened_claims(session, client_id=CLIENT_A).recouped_claims == 0


# ---------------------------------------------------------------------------
# The denial rate must not read a reversal as a denial
# ---------------------------------------------------------------------------


def test_a_reversal_is_not_counted_as_a_denial(archive_root: Path) -> None:
    """A reversal's amounts are the original's negated, and zero negates to zero.

    A line paid nothing originally is still paid nothing once negated, and its
    adjustments come back negated too, so a reversal looks exactly like a
    denial to a query reading lines.

    While the original posting is also loaded this is invisible -- the claim
    was already counted as denied once, so counting it twice changes nothing
    and the first version of this test asserted precisely that non-difference.
    It becomes visible when the reversal arrives for a claim whose original
    remittance was never loaded, which happens whenever history starts
    mid-stream: then the reversal is the *only* evidence, and without the
    exclusion it is read as a denial that never happened.
    """
    submit("CLM1", archive_root, control="000100001")
    submit("CLM2", archive_root, control="000100002")
    # CLM2 is paid normally, so there is something in the denominator.
    remit(payment_for("CLM2"), archive_root, name="pay2.edi")

    # CLM1: only the reversal arrives, and its single line reads as a denial.
    reversed_only = [
        f"CLP*CLM1*22*{-CHARGE}*0.00**MB*PCNCLM1*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{-CHARGE}*0.00**1~",
        f"CAS*CO*16*{-CHARGE}~",
        "AMT*B6*0.00~",
    ]
    remit(_envelope(reversed_only, "0002", Decimal("0.00"), []), archive_root, name="rev.edi")

    with session_scope() as session:
        result = denial_rate(session, client_id=CLIENT_A)
        reversal = session.scalar(select(ClaimPayment).where(ClaimPayment.is_reversal.is_(True)))

    # The reversal was loaded -- it is a real record of money moving.
    assert reversal is not None
    # But nothing was denied. CLM2 was paid and CLM1 was only ever reversed.
    assert result.denied_claims == 0
    assert result.rate == Decimal("0.00")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_reprocessing_a_reversal_is_idempotent(archive_root: Path) -> None:
    """PD-14. Re-presenting the same reversal must not reverse twice."""
    submit("CLM1", archive_root, control="000100001")
    remit(payment_for("CLM1"), archive_root, name="pay.edi")
    payload = reversal_for("CLM1")
    remit(payload, archive_root, name="rev.edi")

    with session_scope() as session:
        before = session.scalar(select(func.count()).select_from(ClaimPayment))

    again = remit(payload, archive_root, name="rev.edi")
    assert again.outcome is LoadOutcome.ALREADY_LOADED

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(ClaimPayment)) == before
        assert reopened_claims(session, client_id=CLIENT_A).total == 1
