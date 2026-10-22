"""Loading an 835 into CORE: ordering, quarantine, idempotency (M1.3, M1.4, M1.8).

Three named invariants from CLAUDE.md section 7.2 live here, and all three are
about something *not* happening:

    test_raw_archived_before_parse        the archive survives a failed parse
    test_835_must_balance_or_quarantine   an unbalanced file loads nothing
    test_reprocessing_835_is_idempotent   re-running changes nothing

Each needs a real database, because each is really a claim about a transaction
boundary, and a mocked session has whatever boundaries the mock was written
with.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from rcm.domain.remittance import (
    ClaimPayment,
    ProviderAdjustment,
    RawFile,
    RawFileStatus,
    Remittance,
    RemittanceAdjustment,
    RemittanceServiceLine,
)
from rcm.ingestion.archive import read_archived
from rcm.ingestion.remittance_loader import (
    ASSERTION_BALANCE,
    ASSERTION_PARSEABLE,
    ASSERTION_SERVICE_LINE_BALANCE,
    ASSERTION_UNIQUE_CONTROL_NUMBER,
    LoadOutcome,
    load_remittance_file,
)
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.integration

CLIENT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
CLIENT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")


def isa(sender: str = "PAYER0001", receiver: str = "PAYEE0001") -> str:
    """Build the ISA from its parts.

    ISA is the one fixed-width segment in X12 -- every delimiter is read from
    it by offset -- so it is assembled rather than produced by substituting
    into a literal. The first version of this helper did the latter and
    silently produced a 107-character ISA, which made every fixture in this
    module unparseable and looked like nine loader failures.
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
        + "*240131*0000*^*00501*000000001*0*T*:~"
    )


def build_835(
    *,
    payment: str = "100.00",
    claims: list[str] | None = None,
    control: str = "0001",
    sender: str = "PAYER0001",
) -> bytes:
    """A valid 835 whose totals can be deliberately broken by the caller."""
    body = [
        f"ST*835*{control}~",
        f"BPR*I*{payment}*C*ACH*CCP*01*999999999*DA*1234*****20240131~",
        "TRN*1*TRACE1*9000~",
        "REF*EV*PAYER0001~",
        "DTM*405*20240131~",
        "N1*PR*Example Health Plan~",
        "N1*PE*A Practice*XX*1234567893~",
        "LX*1~",
        *(
            claims
            if claims is not None
            else [
                "CLP*CLM1*1*200.00*100.00**MB*PCN1*11~",
                "NM1*QC*1*Delgado*Rosa****MI*MEM1~",
                "DTM*232*20240115~",
                "SVC*HC:99213*200.00*100.00**1~",
                "CAS*CO*45*100.00~",
                "AMT*B6*100.00~",
            ]
        ),
    ]
    body.append(f"SE*{len(body) + 1}*{control}~")
    text_835 = (
        isa(sender)
        + f"GS*HP*{sender}*PAYEE0001*20240131*0000*1*X*005010X221A1~"
        + "".join(body)
        + "GE*1*1~IEA*1*000000001~"
    )
    return text_835.encode()


def build_835_with_two_transaction_sets(
    sets_spec: tuple[tuple[str, str, str], ...] = (
        ("0001", "CLM1", "100.00"),
        ("0002", "CLM2", "60.00"),
    ),
) -> bytes:
    """One interchange, one functional group, several ST/SE transaction sets.

    Every file in the synthetic corpus carries exactly one, so this shape is
    otherwise unexercised -- and it is the shape that
    `uq_remittance_raw_file_id_transaction_control_number` exists for: two
    remittances sharing a raw file are told apart only by ST02. A loader that
    ignored the control number would collide here instead of loading both,
    and a bulk 835 from a real payer routinely looks like this.

    `sets_spec` is `(control_number, claim_id, paid_amount)` per transaction
    set, so a caller can also build the malformed version where two sets repeat
    a control number.
    """
    sets: list[str] = []
    for control, claim_id, paid in sets_spec:
        withheld = Decimal("200.00") - Decimal(paid)
        body = [
            f"ST*835*{control}~",
            f"BPR*I*{paid}*C*ACH*CCP*01*999999999*DA*1234*****20240131~",
            f"TRN*1*TRACE{claim_id}*9000~",
            "REF*EV*PAYER0001~",
            "DTM*405*20240131~",
            "N1*PR*Example Health Plan~",
            "N1*PE*A Practice*XX*1234567893~",
            "LX*1~",
            f"CLP*{claim_id}*1*200.00*{paid}**MB*PCN{claim_id}*11~",
            "NM1*QC*1*Delgado*Rosa****MI*MEM1~",
            "DTM*232*20240115~",
            f"SVC*HC:99213*200.00*{paid}**1~",
            f"CAS*CO*45*{withheld}~",
            f"AMT*B6*{paid}~",
        ]
        body.append(f"SE*{len(body) + 1}*{control}~")
        sets.append("".join(body))

    return (
        isa()
        + "GS*HP*PAYER0001*PAYEE0001*20240131*0000*1*X*005010X221A1~"
        + "".join(sets)
        + f"GE*{len(sets)}*1~IEA*1*000000001~"
    ).encode()


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def clean_core(owner_engine: Engine) -> Iterator[None]:
    """Empty the CORE tables around each test.

    Runs as the owner because the cascade reaches `raw_file`, and the
    application role is not the right identity for test housekeeping.
    """

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE remittance_adjustment, remittance_service_line, "
                    "claim_payment, provider_adjustment, remittance, raw_file, "
                    "payer_alias, payer "
                    "RESTART IDENTITY CASCADE"
                )
            )

    reset_engines()
    wipe()
    yield
    wipe()


def counts(session: Session) -> dict[str, int]:
    return {
        "raw_file": session.scalar(select(func.count()).select_from(RawFile)) or 0,
        "remittance": session.scalar(select(func.count()).select_from(Remittance)) or 0,
        "claim": session.scalar(select(func.count()).select_from(ClaimPayment)) or 0,
        "line": session.scalar(select(func.count()).select_from(RemittanceServiceLine)) or 0,
        "adjustment": session.scalar(select(func.count()).select_from(RemittanceAdjustment)) or 0,
    }


# ---------------------------------------------------------------------------
# PD-9: archive before parse
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_raw_archived_before_parse(archive_root: Path) -> None:
    """A file that cannot be parsed is still in RAW afterwards.

    This is the ordering PD-9 requires and the one that is easy to get
    backwards. The tempting shape -- parse, and keep the file if it worked --
    loses exactly the files worth keeping: the one that crashed the parser is
    the one nobody can reproduce, and the payer will not resend it because as
    far as they are concerned they already did.
    """
    garbage = b"ISA*this is not a valid interchange at all~~~"

    with session_scope() as session:
        result = load_remittance_file(
            session,
            garbage,
            client_id=CLIENT_A,
            original_filename="broken.edi",
            archive_root=archive_root,
        )

    assert result.outcome is LoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_PARSEABLE

    # The bytes survived the failure, exactly as received.
    assert read_archived(result.archived.relative_path, archive_root=archive_root) == garbage

    with session_scope() as session:
        assert counts(session)["raw_file"] == 1
        assert counts(session)["remittance"] == 0


def test_the_archived_bytes_are_recoverable_after_a_successful_load(
    archive_root: Path,
) -> None:
    """PD-10: every layer is rebuildable from RAW plus code."""
    payload = build_835()

    with session_scope() as session:
        result = load_remittance_file(
            session,
            payload,
            client_id=CLIENT_A,
            original_filename="good.edi",
            archive_root=archive_root,
        )

    assert result.outcome is LoadOutcome.LOADED
    assert read_archived(result.archived.relative_path, archive_root=archive_root) == payload


# ---------------------------------------------------------------------------
# M1.3: balance or quarantine
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_835_must_balance_or_quarantine(archive_root: Path) -> None:
    """An unbalanced file is quarantined and loads **nothing**.

    Not "the claims that parsed", not "the balanced ones". A file whose
    itemisation does not add up to its own stated total is a file we do not
    understand, and posting the part we happen to understand puts numbers into
    a ledger that money is reconciled against.
    """
    unbalanced = build_835(payment="999.00")  # claims pay 100.00

    with session_scope() as session:
        result = load_remittance_file(
            session,
            unbalanced,
            client_id=CLIENT_A,
            original_filename="unbalanced.edi",
            archive_root=archive_root,
        )

    assert result.outcome is LoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_BALANCE

    with session_scope() as session:
        assert counts(session) == {
            "raw_file": 1,
            "remittance": 0,
            "claim": 0,
            "line": 0,
            "adjustment": 0,
        }
        raw_file = session.scalar(select(RawFile))
        assert raw_file is not None
        assert raw_file.status == str(RawFileStatus.QUARANTINED)
        # DR-10: the quarantine names its assertion and carries the numbers.
        assert raw_file.quarantine_assertion == ASSERTION_BALANCE
        assert raw_file.quarantine_reason
        assert raw_file.quarantine_context is not None
        assert raw_file.quarantine_context["discrepancy"] == "-899.00"


def test_a_service_line_that_does_not_reconcile_is_quarantined(
    archive_root: Path,
) -> None:
    """The claim totals can look right while a line quietly does not add up.

    That is what a dropped adjustment looks like, so it is checked separately
    from the file-level balance.
    """
    payload = build_835(
        claims=[
            "CLP*CLM1*1*200.00*100.00**MB*PCN1*11~",
            "SVC*HC:99213*200.00*100.00**1~",
            "CAS*CO*45*10.00~",  # should be 100.00
            "AMT*B6*100.00~",
        ]
    )

    with session_scope() as session:
        result = load_remittance_file(
            session,
            payload,
            client_id=CLIENT_A,
            original_filename="line-off.edi",
            archive_root=archive_root,
        )

    assert result.quarantine_assertion == ASSERTION_SERVICE_LINE_BALANCE
    with session_scope() as session:
        assert counts(session)["claim"] == 0


def test_a_quarantined_file_is_not_retried_into_a_partial_load(
    archive_root: Path,
) -> None:
    """Re-feeding a quarantined file leaves it quarantined, not half-loaded."""
    unbalanced = build_835(payment="999.00")

    for _ in range(2):
        with session_scope() as session:
            result = load_remittance_file(
                session,
                unbalanced,
                client_id=CLIENT_A,
                original_filename="unbalanced.edi",
                archive_root=archive_root,
            )
        assert result.outcome is LoadOutcome.QUARANTINED

    with session_scope() as session:
        assert counts(session)["raw_file"] == 1
        assert counts(session)["claim"] == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_reprocessing_835_is_idempotent(archive_root: Path) -> None:
    """The M1 gate: re-running the same bytes changes nothing.

    Satisfied by recognising the file rather than by making the load itself
    repeatable -- it is not, because every row carries a generated key.
    """
    payload = build_835()

    with session_scope() as session:
        first = load_remittance_file(
            session,
            payload,
            client_id=CLIENT_A,
            original_filename="r.edi",
            archive_root=archive_root,
        )
    with session_scope() as session:
        after_first = counts(session)

    with session_scope() as session:
        second = load_remittance_file(
            session,
            payload,
            client_id=CLIENT_A,
            original_filename="r-again-renamed.edi",
            archive_root=archive_root,
        )
    with session_scope() as session:
        after_second = counts(session)

    assert first.outcome is LoadOutcome.LOADED
    assert second.outcome is LoadOutcome.ALREADY_LOADED
    assert second.claims_loaded == 0
    assert after_first == after_second, "a second load changed the database"


# ---------------------------------------------------------------------------
# PD-6 in the database
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_service_line_grain_not_collapsed(archive_root: Path) -> None:
    """Three CAS reasons on one line become three rows in CORE.

    The parser keeping them apart is necessary and not sufficient -- the
    loader is the other place they can be merged.
    """
    payload = build_835(
        payment="0.00",
        claims=[
            "CLP*CLM1*4*300.00*0.00**MB*PCN1*11~",
            "SVC*HC:99213*300.00*0.00**1~",
            "CAS*CO*45*120.00**97*100.00**18*80.00~",
            "AMT*B6*0.00~",
        ],
    )

    with session_scope() as session:
        load_remittance_file(
            session,
            payload,
            client_id=CLIENT_A,
            original_filename="multi.edi",
            archive_root=archive_root,
        )

    with session_scope() as session:
        rows = list(
            session.scalars(select(RemittanceAdjustment).order_by(RemittanceAdjustment.sequence))
        )
        assert len(rows) == 3
        assert [r.reason_code for r in rows] == ["45", "97", "18"]
        assert [r.amount for r in rows] == [
            Decimal("120.00"),
            Decimal("100.00"),
            Decimal("80.00"),
        ]
        assert all(r.cas_group_code == "CO" for r in rows)
        assert all(r.service_line_id is not None for r in rows)
        assert session.scalar(select(func.count()).select_from(RemittanceServiceLine)) == 1


def test_claim_level_adjustments_are_stored_without_a_service_line(
    archive_root: Path,
) -> None:
    # The line balances on its own terms (200 = 170 + 30). The claim pays 20
    # less than its lines because of the claim-level OA adjustment, which is
    # exactly the shape that makes the two levels distinguishable.
    payload = build_835(
        payment="150.00",
        claims=[
            "CLP*CLM1*1*200.00*150.00**MB*PCN1*11~",
            "CAS*OA*23*20.00~",
            "SVC*HC:99213*200.00*170.00**1~",
            "CAS*CO*45*30.00~",
            "AMT*B6*170.00~",
        ],
    )

    with session_scope() as session:
        load_remittance_file(
            session,
            payload,
            client_id=CLIENT_A,
            original_filename="claim-level.edi",
            archive_root=archive_root,
        )

    with session_scope() as session:
        claim_level = list(
            session.scalars(
                select(RemittanceAdjustment).where(RemittanceAdjustment.claim_level.is_(True))
            )
        )
        line_level = list(
            session.scalars(
                select(RemittanceAdjustment).where(RemittanceAdjustment.claim_level.is_(False))
            )
        )
        assert len(claim_level) == 1
        assert claim_level[0].service_line_id is None
        assert claim_level[0].cas_group_code == "OA"
        assert len(line_level) == 1
        assert line_level[0].service_line_id is not None


def test_provider_level_adjustments_are_stored(archive_root: Path) -> None:
    payload = build_835(
        payment="75.00",
        claims=[
            "CLP*CLM1*1*200.00*100.00**MB*PCN1*11~",
            "SVC*HC:99213*200.00*100.00**1~",
            "CAS*CO*45*100.00~",
            "AMT*B6*100.00~",
            "PLB*1234567893*20241231*WO:PCN0099*25.00~",
        ],
    )

    with session_scope() as session:
        load_remittance_file(
            session,
            payload,
            client_id=CLIENT_A,
            original_filename="plb.edi",
            archive_root=archive_root,
        )

    with session_scope() as session:
        plb = session.scalar(select(ProviderAdjustment))
        assert plb is not None
        assert plb.amount == Decimal("25.00")
        assert plb.reason_code == "WO"
        assert plb.reference == "PCN0099"


# ---------------------------------------------------------------------------
# PD-20 and PD-8
# ---------------------------------------------------------------------------


def test_every_core_row_carries_the_client_it_belongs_to(archive_root: Path) -> None:
    """PD-20 needs `client_id` on the entity being queried, not through a join."""
    with session_scope() as session:
        load_remittance_file(
            session,
            build_835(),
            client_id=CLIENT_B,
            original_filename="b.edi",
            archive_root=archive_root,
        )

    with session_scope() as session:
        for model in (
            RawFile,
            Remittance,
            ClaimPayment,
            RemittanceServiceLine,
            RemittanceAdjustment,
        ):
            rows = list(session.scalars(select(model)))
            assert rows, f"{model.__tablename__} has no rows"
            assert all(row.client_id == CLIENT_B for row in rows), model.__tablename__


def test_the_payer_is_keyed_on_its_identifier_not_its_name(archive_root: Path) -> None:
    """PD-8. Two files from the same payer under different display names
    resolve to one payer, because the identifier is what identity keys on."""
    from rcm.domain.payer import Payer

    first = build_835()
    second = build_835(control="0002").replace(
        b"N1*PR*Example Health Plan~", b"N1*PR*EXAMPLE HEALTH PLAN INC~"
    )

    for index, payload in enumerate((first, second)):
        with session_scope() as session:
            load_remittance_file(
                session,
                payload,
                client_id=CLIENT_A,
                original_filename=f"p{index}.edi",
                archive_root=archive_root,
            )

    with session_scope() as session:
        payers = list(session.scalars(select(Payer)))
        assert len(payers) == 1, f"one payer became {len(payers)}: {[p.name for p in payers]}"
        assert payers[0].payer_id == "PAYER0001"
        # The first name seen is kept; a later spelling does not overwrite it,
        # or every report that quoted the old one becomes unreproducible.
        assert payers[0].name == "Example Health Plan"


def test_two_transaction_sets_in_one_file_load_as_two_remittances(archive_root: Path) -> None:
    """A bulk 835 is one archived file and several remittances.

    Asserted against the database rather than the parser, because the question
    is whether the CORE schema can hold the result: one `raw_file` row, two
    `remittance` rows distinguished by their transaction control numbers, and
    the claims hanging off the right one.
    """
    with session_scope() as session:
        result = load_remittance_file(
            session,
            build_835_with_two_transaction_sets(),
            client_id=CLIENT_A,
            original_filename="bulk.edi",
            archive_root=archive_root,
        )

    assert result.outcome is LoadOutcome.LOADED

    with session_scope() as session:
        totals = counts(session)
        assert totals["raw_file"] == 1
        assert totals["remittance"] == 2
        assert totals["claim"] == 2
        assert set(session.scalars(select(Remittance.transaction_control_number))) == {
            "0001",
            "0002",
        }
        # Each remittance carries its own BPR02, not the file's total.
        assert set(session.scalars(select(Remittance.payment_amount))) == {
            Decimal("100.00"),
            Decimal("60.00"),
        }
        # And each claim is attached to the transaction set that paid it.
        paired = session.execute(
            select(ClaimPayment.claim_id, Remittance.transaction_control_number).join(
                Remittance, ClaimPayment.remittance_id == Remittance.remittance_id
            )
        ).all()
        assert set(paired) == {("CLM1", "0001"), ("CLM2", "0002")}


def test_a_repeated_transaction_control_number_is_quarantined(archive_root: Path) -> None:
    """A malformed bulk 835 names its assertion instead of raising.

    ST02 is the only thing separating two remittances that share a raw file, so
    a file repeating it cannot be stored. Before IR-04 existed the uniqueness
    violation surfaced as an unhandled `IntegrityError` from the load, which
    rolled back the whole transaction -- the `raw_file` row with it. The bytes
    sat in the archive with nothing recording why they had failed, so the file
    was neither loaded nor quarantined, breaking PD-9 and DR-10 at once.
    """
    payload = build_835_with_two_transaction_sets(
        (("0001", "CLM1", "100.00"), ("0001", "CLM2", "60.00")),
    )

    with session_scope() as session:
        result = load_remittance_file(
            session,
            payload,
            client_id=CLIENT_A,
            original_filename="repeated-control.edi",
            archive_root=archive_root,
        )

    assert result.outcome is LoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_UNIQUE_CONTROL_NUMBER

    with session_scope() as session:
        totals = counts(session)
        # The record of the failure survives -- that is the half that was lost.
        assert totals["raw_file"] == 1
        assert totals["remittance"] == 0
        assert totals["claim"] == 0
        quarantined = session.scalars(select(RawFile)).one()
        assert quarantined.status == str(RawFileStatus.QUARANTINED)
        assert quarantined.quarantine_assertion == ASSERTION_UNIQUE_CONTROL_NUMBER

    # And the bytes are still readable, as PD-9 requires.
    assert read_archived(result.archived.relative_path, archive_root=archive_root) == payload
