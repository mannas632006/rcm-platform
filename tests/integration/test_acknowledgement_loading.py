"""Acknowledgements in the database, and the line PD-4 draws (M1.6).

    PD-4: Rejections and denials are distinct outcome types. Never conflated.

    BR-GLOBAL-02 (MUST): Rejections and denials MUST be stored and reported
    as distinct outcome types. Conflating them inflates the denial rate and
    hides front-end data quality problems.

The invariant this file exists for is `test_rejection_is_not_denial`, and the
argument it makes is structural rather than procedural. Rejections land in
`claim_acknowledgement`. Denials are derived from `claim_payment`,
`remittance_service_line` and `remittance_adjustment`. The denial query does
not read the acknowledgement table, so a rejection cannot reach the denial
rate however that query is later rewritten -- there is no join that would
bring it in by accident.

That is a stronger guarantee than a flag on a shared table, where every future
query is one forgotten `WHERE` away from counting a rejection as a denial, and
it is the same argument the remittance loader makes for never creating a
claim: a rule nobody has to remember beats a rule everybody must.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text

from rcm.analytics.denials import denial_rate
from rcm.domain.acknowledgement import (
    AcknowledgementOutcome,
    AcknowledgementSource,
    ClaimAcknowledgement,
)
from rcm.domain.remittance import ClaimPayment, RawFile, RawFileStatus
from rcm.ingestion.acknowledgement_loader import (
    ASSERTION_CONTAINS_ACKNOWLEDGEMENT,
    ASSERTION_KNOWN_STATUS_CATEGORY,
    AcknowledgementLoadOutcome,
    load_acknowledgement_file,
)
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.remittance_loader import LoadOutcome, load_remittance_file
from rcm.platform.db import reset_engines, session_scope
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.integration

CLIENT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
CLIENT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")
CHARGE = Decimal("200.00")
PAID = Decimal("150.00")


# ---------------------------------------------------------------------------
# Fixtures: the three files a claim meets on its way through
# ---------------------------------------------------------------------------


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


def _wrap(body: list[str], *, functional: str, version: str, control: str) -> bytes:
    segments = [*body, f"SE*{len(body) + 1}*{control}~"]
    return (
        isa()
        + f"GS*{functional}*CLEARINGHSE1*SUBMITTER01*20240131*0000*1*X*{version}~"
        + "".join(segments)
        + "GE*1*1~IEA*1*000000001~"
    ).encode()


def ack_277ca(*statuses: tuple[str, str], control: str = "0001") -> bytes:
    """A 277CA verdict on each of `statuses`, given as (claim_id, "A3:21")."""
    body = [
        f"ST*277*{control}*005010X214~",
        f"BHT*0085*08*{control}*20240131*0000*TH~",
        "HL*1**20*1~",
        "NM1*PR*2*BCBS Texas*****PI*00590~",
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
                f"STC*{composite}*20240131*WQ*{CHARGE}********Returned unprocessable~",
                f"REF*1K*PCN{claim_id}~",
            ]
        )
        level += 1
    return _wrap(body, functional="HN", version="005010X214", control=control)


def ack_999(*, status: str = "A", control: str = "0001", group: str = "100072") -> bytes:
    body = [
        f"ST*999*{control}*005010X231A1~",
        f"AK1*HC*{group}*005010X222A1~",
        "AK2*837*0001*005010X222A1~",
        f"IK5*{status}~",
        f"AK9*{status}*1*1*{'1' if status == 'A' else '0'}~",
    ]
    return _wrap(body, functional="FA", version="005010X231A1", control=control)


def _remittance(body: list[str], *, control: str, check: Decimal) -> bytes:
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
    ]
    header.append(f"SE*{len(header) + 1}*{control}~")
    return (
        isa("PAYER0001", "PAYEE0001")
        + "GS*HP*PAYER0001*PAYEE0001*20240131*0000*1*X*005010X221A1~"
        + "".join(header)
        + "GE*1*1~IEA*1*000000001~"
    ).encode()


def denial_for(claim_id: str, *, control: str) -> bytes:
    """An 835 that adjudicated the claim and refused to pay it.

    Charged 200, paid nothing, the whole amount adjusted under CO-50 with a
    reason code. Both halves matter: a zero payment without a reason is not a
    denial, and the balance assertion needs `charge == paid + adjustments`.
    """
    body = [
        f"CLP*{claim_id}*4*{CHARGE}*0.00**MB*PCN{claim_id}*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*0.00**1~",
        f"CAS*CO*50*{CHARGE}~",
        "AMT*B6*0.00~",
    ]
    return _remittance(body, control=control, check=Decimal("0.00"))


def payment_for(claim_id: str, *, control: str) -> bytes:
    body = [
        f"CLP*{claim_id}*1*{CHARGE}*{PAID}**MB*PCN{claim_id}*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*{PAID}**1~",
        f"CAS*CO*45*{CHARGE - PAID}~",
        f"AMT*B6*{PAID}~",
    ]
    return _remittance(body, control=control, check=PAID)


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
                    "TRUNCATE claim_acknowledgement, claim_line, claim, "
                    "remittance_adjustment, remittance_service_line, claim_payment, "
                    "provider_adjustment, remittance, raw_file, payer_alias, payer "
                    "RESTART IDENTITY CASCADE"
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


def acknowledge(payload: bytes, root: Path, *, client: uuid.UUID = CLIENT_A, name: str):
    with session_scope() as session:
        return load_acknowledgement_file(
            session, payload, client_id=client, original_filename=name, archive_root=root
        )


def remit(payload: bytes, root: Path, *, client: uuid.UUID = CLIENT_A, name: str):
    with session_scope() as session:
        return load_remittance_file(
            session, payload, client_id=client, original_filename=name, archive_root=root
        )


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_rejection_is_not_denial(archive_root: Path) -> None:
    """PD-4 / BR-GLOBAL-02, measured on three claims with three fates.

    REJECT was returned by the payer's front end and never adjudicated.
    DENY was adjudicated and refused. PAY was adjudicated and paid.

    The denial rate must see two claims and one denial. A platform that reads
    a rejection as a denial reports three and two -- 66.67% instead of 50% --
    and the number is indistinguishable from a real deterioration in payer
    behaviour, so nobody downstream can detect the error.
    """
    submit("REJECT", archive_root, control="0001")
    submit("DENY", archive_root, control="0002")
    submit("PAY", archive_root, control="0003")

    acknowledge(
        ack_277ca(("REJECT", "A3:21"), ("DENY", "A2:20"), ("PAY", "A2:20")),
        archive_root,
        name="277ca.edi",
    )
    assert remit(denial_for("DENY", control="0010"), archive_root, name="deny.835").outcome is (
        LoadOutcome.LOADED
    )
    assert remit(payment_for("PAY", control="0011"), archive_root, name="pay.835").outcome is (
        LoadOutcome.LOADED
    )

    with session_scope() as session:
        result = denial_rate(session, client_id=CLIENT_A)

        # Two adjudicated claims, one denied. REJECT is in neither figure:
        # not the numerator, because it was not denied, and not the
        # denominator, because it never reached adjudication.
        assert result.claims == 2
        assert result.denied_claims == 1
        assert result.rate == Decimal("50.00")

        # The rejection is recorded -- it is not lost, it is elsewhere.
        rejections = session.scalars(
            select(ClaimAcknowledgement).where(
                ClaimAcknowledgement.outcome == str(AcknowledgementOutcome.REJECTED)
            )
        ).all()
        assert [r.claim_id for r in rejections] == ["REJECT"]
        assert rejections[0].category_code == "A3"

        # And structurally out of reach: the denial query reads `claim_payment`,
        # and a rejected claim has no row there to find.
        assert (
            session.scalar(
                select(func.count())
                .select_from(ClaimPayment)
                .where(ClaimPayment.claim_id == "REJECT")
            )
            == 0
        )


@pytest.mark.invariant
def test_a_rejected_claim_is_not_counted_as_adjudicated(archive_root: Path) -> None:
    """The denominator, separately from the numerator.

    Counting rejections as adjudicated *lowers* the denial rate, which is the
    direction nobody checks. One denial out of one adjudicated claim is 100%;
    adding a rejected claim to the denominator reports 50% and looks like good
    news.
    """
    submit("DENY", archive_root, control="0001")
    submit("REJECT", archive_root, control="0002")
    acknowledge(ack_277ca(("DENY", "A2:20"), ("REJECT", "A6:21")), archive_root, name="277ca.edi")
    remit(denial_for("DENY", control="0010"), archive_root, name="deny.835")

    with session_scope() as session:
        result = denial_rate(session, client_id=CLIENT_A)
        assert result.claims == 1
        assert result.rate == Decimal("100.00")


@pytest.mark.invariant
def test_999_acceptance_is_not_payer_acceptance(archive_root: Path) -> None:
    """BR-M8-02: 999 acceptance confirms syntax, not acceptance.

    The two outcomes are the same word and mean different things, so they are
    told apart by `source` rather than by outcome. A query answering "is this
    claim submitted" that cannot distinguish them will answer yes for a claim
    the payer has not yet seen.
    """
    submit("CLM1", archive_root, control="0001")
    acknowledge(ack_999(status="A"), archive_root, name="999.edi")

    with session_scope() as session:
        rows = session.scalars(select(ClaimAcknowledgement)).all()
        assert [r.source for r in rows] == [str(AcknowledgementSource.SYNTAX_999)]
        assert rows[0].outcome == str(AcknowledgementOutcome.ACCEPTED)

        # Nothing from a 999 is a 277CA acceptance, so the question
        # "accepted by the payer" has no row to answer it yes.
        assert (
            session.scalar(
                select(func.count())
                .select_from(ClaimAcknowledgement)
                .where(
                    ClaimAcknowledgement.source == str(AcknowledgementSource.CLAIM_277CA),
                    ClaimAcknowledgement.outcome == str(AcknowledgementOutcome.ACCEPTED),
                )
            )
            == 0
        )


# ---------------------------------------------------------------------------
# Loading, quarantine and idempotency
# ---------------------------------------------------------------------------


def test_a_277ca_loads_a_verdict_per_claim(archive_root: Path) -> None:
    result = acknowledge(
        ack_277ca(("C1", "A2:20"), ("C2", "A3:21"), ("C3", "A2:20")),
        archive_root,
        name="277ca.edi",
    )
    assert result.outcome is AcknowledgementLoadOutcome.LOADED
    assert (result.accepted, result.rejected) == (2, 1)

    with session_scope() as session:
        rows = session.scalars(
            select(ClaimAcknowledgement).order_by(ClaimAcknowledgement.claim_id)
        ).all()
        assert [(r.claim_id, r.outcome) for r in rows] == [
            ("C1", "ACCEPTED"),
            ("C2", "REJECTED"),
            ("C3", "ACCEPTED"),
        ]
        assert rows[1].payer_claim_number == "PCNC2"
        assert rows[1].description == "Returned unprocessable"


@pytest.mark.invariant
def test_an_unknown_status_category_quarantines_the_file(archive_root: Path) -> None:
    """PD-18, DR-10. A category outside both code lists has no verdict this
    platform can read, and the recoverable failure is to stop with the code
    named. Guessing would give a claim an outcome nobody chose."""
    result = acknowledge(ack_277ca(("C1", "ZZ:99")), archive_root, name="277ca.edi")

    assert result.quarantined
    assert result.quarantine_assertion == ASSERTION_KNOWN_STATUS_CATEGORY

    with session_scope() as session:
        # Quarantine is all-or-nothing: the file named one unreadable claim
        # and no verdict from it was kept.
        assert session.scalar(select(func.count()).select_from(ClaimAcknowledgement)) == 0
        raw = session.scalars(select(RawFile)).one()
        assert raw.status == str(RawFileStatus.QUARANTINED)
        assert "ZZ" in str(raw.quarantine_context)


def test_one_unreadable_category_quarantines_the_whole_file(archive_root: Path) -> None:
    """Partial loading would leave the readable claims in and the unreadable
    one out, with nothing recording the difference (PD-9/PD-10: the file is
    rebuilt from RAW once the code list is extended)."""
    result = acknowledge(
        ack_277ca(("C1", "A2:20"), ("C2", "QQ:99")), archive_root, name="277ca.edi"
    )
    assert result.quarantined
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(ClaimAcknowledgement)) == 0


def test_a_file_with_no_acknowledgement_transaction_quarantines(archive_root: Path) -> None:
    payload = _wrap(
        ["ST*820*0001~", "BPR*I*100.00*C*ACH~"],
        functional="RA",
        version="005010X218",
        control="0001",
    )
    result = acknowledge(payload, archive_root, name="820.edi")
    assert result.quarantined
    assert result.quarantine_assertion == ASSERTION_CONTAINS_ACKNOWLEDGEMENT


@pytest.mark.invariant
def test_reprocessing_an_acknowledgement_is_idempotent(archive_root: Path) -> None:
    """PD-14. Re-presenting a file is normal -- a clearinghouse redelivers,
    an operator replays a day -- and it must change nothing."""
    payload = ack_277ca(("C1", "A2:20"), ("C2", "A3:21"))

    first = acknowledge(payload, archive_root, name="277ca.edi")
    assert first.outcome is AcknowledgementLoadOutcome.LOADED

    second = acknowledge(payload, archive_root, name="277ca-again.edi")
    assert second.outcome is AcknowledgementLoadOutcome.ALREADY_LOADED
    assert second.raw_file_id == first.raw_file_id

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(ClaimAcknowledgement)) == 2
        assert session.scalar(select(func.count()).select_from(RawFile)) == 1


def test_the_same_verdict_twice_in_one_file_is_one_fact(archive_root: Path) -> None:
    """A clearinghouse repeating a status within a file is not two verdicts,
    and the table's uniqueness constraint would refuse the second row."""
    result = acknowledge(
        ack_277ca(("C1", "A3:21"), ("C1", "A3:21")), archive_root, name="277ca.edi"
    )
    assert result.outcome is AcknowledgementLoadOutcome.LOADED
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(ClaimAcknowledgement)) == 1


def test_two_verdicts_on_one_claim_from_different_sources_both_persist(
    archive_root: Path,
) -> None:
    """A 999 acceptance and a 277CA rejection on the same claim is the normal
    sequence, not a contradiction: the syntax was fine and the payer still
    returned it (BR-M8-02)."""
    acknowledge(ack_999(status="A", control="0001"), archive_root, name="999.edi")
    acknowledge(ack_277ca(("0001", "A3:21"), control="0002"), archive_root, name="277ca.edi")

    with session_scope() as session:
        rows = session.scalars(
            select(ClaimAcknowledgement)
            .where(ClaimAcknowledgement.claim_id == "0001")
            .order_by(ClaimAcknowledgement.source)
        ).all()
        assert [(r.source, r.outcome) for r in rows] == [
            ("277CA", "REJECTED"),
            ("999", "ACCEPTED"),
        ]


def test_a_271_is_parsed_and_reported_but_not_stored(archive_root: Path) -> None:
    """Eligibility belongs to M3's module. Counting it proves the file was
    read; a table nothing reads yet is a table that rots."""
    payload = _wrap(
        [
            "ST*271*0001*005010X279A1~",
            "BHT*0022*11*0001*20240131*0000~",
            "HL*1**20*1~",
            "NM1*PR*2*BCBS Texas*****PI*00590~",
            "HL*2*1*22*0~",
            "NM1*IL*1*Castellanos*Andre****MI*VAZ957419823~",
            "EB*1*IND*30**Gold PPO~",
        ],
        functional="HB",
        version="005010X279A1",
        control="0001",
    )
    result = acknowledge(payload, archive_root, name="271.edi")

    assert result.outcome is AcknowledgementLoadOutcome.LOADED
    assert result.eligibility_responses == 1
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(ClaimAcknowledgement)) == 0


@pytest.mark.invariant
def test_acknowledgements_are_scoped_to_their_client(archive_root: Path) -> None:
    """PD-20. Two clients can be sent the same claim control number by
    different clearinghouses, so the id alone must never be enough to read a
    row."""
    acknowledge(ack_277ca(("SHARED", "A3:21"), control="0001"), archive_root, name="a.edi")
    acknowledge(
        ack_277ca(("SHARED", "A2:20"), control="0002"),
        archive_root,
        client=CLIENT_B,
        name="b.edi",
    )

    with session_scope() as session:
        for client, expected in ((CLIENT_A, "REJECTED"), (CLIENT_B, "ACCEPTED")):
            rows = session.scalars(
                select(ClaimAcknowledgement).where(ClaimAcknowledgement.client_id == client)
            ).all()
            assert [r.outcome for r in rows] == [expected]


def test_an_acknowledgement_for_an_unknown_claim_is_still_kept(archive_root: Path) -> None:
    """`claim_id` is text, not a foreign key, deliberately. A rejection can
    arrive for a claim this platform never loaded -- another biller's
    submission under the same submitter id, a claim ingested later -- and
    dropping the rejection because its claim is missing loses the only record
    that the claim needs resending."""
    result = acknowledge(ack_277ca(("NEVERSEEN", "A7:21")), archive_root, name="277ca.edi")
    assert (result.accepted, result.rejected) == (0, 1)

    with session_scope() as session:
        row = session.scalars(select(ClaimAcknowledgement)).one()
        assert row.claim_id == "NEVERSEEN"


def test_the_raw_file_is_archived_before_it_is_parsed(archive_root: Path) -> None:
    """PD-9, for this loader too: the bytes of a file that quarantines are on
    disk, which is what makes PD-10 reconstruction possible."""
    result = acknowledge(ack_277ca(("C1", "ZZ:99")), archive_root, name="277ca.edi")
    assert result.quarantined

    stored = archive_root / result.archived.relative_path
    assert stored.exists()
    assert stored.read_bytes() == ack_277ca(("C1", "ZZ:99"))
