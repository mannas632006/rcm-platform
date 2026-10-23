"""Loading 837 claims into CORE, and chaining resubmissions (M1.9).

The invariant that matters here is BR-DATA-03 / PD-7: a resubmission is not a
new claim. Every test below is really one question asked differently -- after
loading, how many *claims* does the platform think it has?

Fixtures are built with `build_837` rather than hand-written X12. That is the
generator from M1.5, which the round-trip and corpus suites already hold to the
format, so a failure here is a loader failure rather than a fixture typo. The
835 work learned that distinction the hard way, through a 107-character ISA.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim, ClaimLine
from rcm.domain.remittance import RawFile, RawFileStatus
from rcm.ingestion.archive import read_archived
from rcm.ingestion.claim_loader import (
    ASSERTION_CHARGES_RECONCILE,
    ASSERTION_CONTAINS_CLAIM,
    ASSERTION_DIAGNOSIS_POINTER,
    ASSERTION_KNOWN_ORIGINAL,
    ASSERTION_PARSEABLE,
    ClaimLoadOutcome,
    load_claim_file,
)
from rcm.ingestion.x12 import (
    BillingProvider,
    Claim837,
    ClaimServiceLine,
    ClaimSubmission,
    Subscriber,
    TradingPartner,
    build_837,
)
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.integration

CLIENT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
CLIENT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")

PROVIDER = BillingProvider(
    name="Lakeview Clinic", npi="4952654158", taxonomy_code="207Q00000X", tax_id="549656931"
)
SUBSCRIBER = Subscriber(
    last_name="Castellanos", first_name="Andre", member_id="VAZ957419823", sex="F"
)


def claim(
    claim_id: str,
    *,
    original: str = "",
    charge: str = "186.49",
    lines: tuple[ClaimServiceLine, ...] | None = None,
    diagnoses: tuple[str, ...] = ("J06.9", "E11.65"),
    frequency: str = "1",
) -> Claim837:
    return Claim837(
        claim_id=claim_id,
        total_charge=Decimal(charge),
        billing_provider=PROVIDER,
        subscriber=SUBSCRIBER,
        payer_id="00590",
        payer_name="BCBS Texas",
        claim_filing_code="CI",
        place_of_service="11",
        claim_frequency_code=frequency,
        diagnoses=diagnoses,
        service_date=date(2024, 1, 1),
        original_claim_id=original,
        lines=lines
        if lines is not None
        else (
            ClaimServiceLine(
                line_number=1,
                procedure_code="99203",
                charge_amount=Decimal(charge),
                diagnosis_pointers=(1,),
                service_date=date(2024, 1, 1),
            ),
        ),
    )


def file_for(*claims: Claim837, control: str = "000100026") -> bytes:
    return build_837(
        ClaimSubmission(
            control_number=control,
            submitter=TradingPartner(name="RCM SEED BILLING", etin="SUBMITTER01"),
            receiver=TradingPartner(name="SEED CLEARINGHOUSE", etin="CLEARINGHSE1"),
            claims=claims,
            created_on=date(2024, 1, 1),
        )
    ).encode()


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def clean_core(owner_engine: Engine) -> Iterator[None]:
    """Empty the claim and remittance tables around each test.

    Truncated at setup as well as teardown: a test that leaves the database in
    an unexpected shape should not be able to make every later test inherit it.
    """

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


def counts(session: Session) -> dict[str, int]:
    return {
        "claim": session.scalar(select(func.count()).select_from(Claim)) or 0,
        "distinct": session.scalar(select(func.count(func.distinct(Claim.root_claim_id)))) or 0,
        "line": session.scalar(select(func.count()).select_from(ClaimLine)) or 0,
        "raw_file": session.scalar(select(func.count()).select_from(RawFile)) or 0,
    }


def load(payload: bytes, archive_root: Path, *, client: uuid.UUID = CLIENT_A, name: str = "c.edi"):
    with session_scope() as session:
        return load_claim_file(
            session,
            payload,
            client_id=client,
            original_filename=name,
            archive_root=archive_root,
        )


# ---------------------------------------------------------------------------
# PD-7 / BR-DATA-03
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_resubmission_chains_to_original(archive_root: Path) -> None:
    """The chain reaches CORE, not just the parsed object.

    M1.5 proved `REF*F8` survives parsing. This is the half that matters for
    reporting: the second submission must attach to the first claim rather
    than becoming a claim of its own.
    """
    load(file_for(claim("CLM1")), archive_root, name="first.edi")
    load(file_for(claim("CLM1R1", original="CLM1", frequency="7")), archive_root, name="re.edi")

    with session_scope() as session:
        totals = counts(session)
        assert totals["claim"] == 2  # two submissions
        assert totals["distinct"] == 1  # of one claim

        original = session.scalar(select(Claim).where(Claim.submitter_claim_id == "CLM1"))
        resubmission = session.scalar(select(Claim).where(Claim.submitter_claim_id == "CLM1R1"))
        assert original is not None
        assert resubmission is not None

        assert original.original_claim_id is None
        assert original.root_claim_id == original.claim_id
        assert original.submission_attempt == 1

        assert resubmission.original_claim_id == original.claim_id
        assert resubmission.root_claim_id == original.claim_id
        assert resubmission.submission_attempt == 2
        assert resubmission.claim_frequency_code == "7"


@pytest.mark.invariant
def test_three_submissions_of_one_claim_count_as_one_claim(archive_root: Path) -> None:
    """M1.9's acceptance condition, stated verbatim in CLAUDE.md.

    The corpus only ever chains two deep, so this is the test that covers a
    chain long enough for the root to differ from the parent -- which is where
    a loader that sets `root = parent` instead of `root = parent.root` starts
    reporting two claims instead of one.
    """
    load(file_for(claim("CLM1")), archive_root, name="a.edi")
    load(file_for(claim("CLM1R1", original="CLM1")), archive_root, name="b.edi")
    load(file_for(claim("CLM1R2", original="CLM1R1")), archive_root, name="c.edi")

    with session_scope() as session:
        totals = counts(session)
        assert totals["claim"] == 3
        assert totals["distinct"] == 1

        chain = {c.submitter_claim_id: c for c in session.scalars(select(Claim)).all()}
        root_id = chain["CLM1"].claim_id
        # Every link roots at the *first* submission, not at its own parent.
        assert [chain[c].root_claim_id for c in ("CLM1", "CLM1R1", "CLM1R2")] == [root_id] * 3
        assert chain["CLM1R2"].original_claim_id == chain["CLM1R1"].claim_id
        assert [chain[c].submission_attempt for c in ("CLM1", "CLM1R1", "CLM1R2")] == [1, 2, 3]


def test_a_resubmission_in_the_same_file_as_its_original_chains(archive_root: Path) -> None:
    """Both claims in one transaction, the resubmission second."""
    load(file_for(claim("CLM1"), claim("CLM1R1", original="CLM1")), archive_root)

    with session_scope() as session:
        assert counts(session) == {"claim": 2, "distinct": 1, "line": 2, "raw_file": 1}


def test_unrelated_claims_stay_unrelated(archive_root: Path) -> None:
    """A claim with no REF*F8 is its own root, and two of them are two claims."""
    load(file_for(claim("CLM1"), claim("CLM2")), archive_root)

    with session_scope() as session:
        totals = counts(session)
        assert totals["claim"] == 2
        assert totals["distinct"] == 2


# ---------------------------------------------------------------------------
# Refusals, each named
# ---------------------------------------------------------------------------


def test_a_resubmission_naming_an_unknown_claim_is_quarantined(archive_root: Path) -> None:
    """IR-05. Loading it as an original would inflate the claim count silently.

    Treating the orphan as a new claim is the tempting recovery and it is the
    exact failure BR-DATA-03 names: volume goes up, the denial rate moves, and
    every number stays internally consistent.
    """
    result = load(file_for(claim("CLM1R1", original="CLM-NEVER-SEEN")), archive_root)

    assert result.outcome is ClaimLoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_KNOWN_ORIGINAL

    with session_scope() as session:
        totals = counts(session)
        assert totals["claim"] == 0
        assert totals["line"] == 0
        # The record of the failure survives, with the reason named.
        assert totals["raw_file"] == 1


def test_an_out_of_order_file_succeeds_once_its_original_arrives(archive_root: Path) -> None:
    """The recovery path for IR-05, which is why quarantining is safe.

    Files legitimately arrive out of order. The bytes are in RAW, so once the
    original lands the orphan can be re-presented and loads normally -- a
    quarantine that resolves itself on retry, rather than a wrong number that
    never surfaces.
    """
    orphan = file_for(claim("CLM1R1", original="CLM1"), control="000100027")
    first = load(orphan, archive_root, name="orphan.edi")
    assert first.outcome is ClaimLoadOutcome.QUARANTINED

    load(file_for(claim("CLM1")), archive_root, name="original.edi")

    # Re-presented under a different name; the bytes, and so the hash, differ
    # from the original file, so this is a genuine second attempt.
    retry = load(
        file_for(claim("CLM1R1", original="CLM1"), control="000100028"),
        archive_root,
        name="orphan-again.edi",
    )
    assert retry.outcome is ClaimLoadOutcome.LOADED
    assert retry.resubmissions_chained == 1

    with session_scope() as session:
        totals = counts(session)
        assert totals["claim"] == 2
        assert totals["distinct"] == 1


def test_a_claim_whose_total_disagrees_with_its_lines_is_quarantined(
    archive_root: Path,
) -> None:
    """IR-07, the claim-side counterpart of the 835 balance assertion."""
    broken = claim(
        "CLM1",
        charge="500.00",
        lines=(
            ClaimServiceLine(
                line_number=1,
                procedure_code="99203",
                charge_amount=Decimal("100.00"),
                diagnosis_pointers=(1,),
            ),
        ),
    )
    result = load(file_for(broken), archive_root)

    assert result.outcome is ClaimLoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_CHARGES_RECONCILE
    with session_scope() as session:
        assert counts(session)["claim"] == 0


def test_a_line_without_a_diagnosis_pointer_is_quarantined(archive_root: Path) -> None:
    """IR-08 / BR-M6-01."""
    broken = claim(
        "CLM1",
        lines=(
            ClaimServiceLine(
                line_number=1,
                procedure_code="99203",
                charge_amount=Decimal("186.49"),
                diagnosis_pointers=(),
            ),
        ),
    )
    result = load(file_for(broken), archive_root)

    assert result.outcome is ClaimLoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_DIAGNOSIS_POINTER
    with session_scope() as session:
        assert counts(session)["claim"] == 0


def test_a_file_with_no_claims_is_quarantined(archive_root: Path) -> None:
    """IR-06. An 835 handed to the claim loader is not silently a no-op."""
    remittance = (
        b"ISA*00*          *00*          *ZZ*PAYER0001      *ZZ*PAYEE0001      "
        b"*240131*0000*^*00501*000000001*0*T*:~"
        b"GS*HP*PAYER0001*PAYEE0001*20240131*0000*1*X*005010X221A1~"
        b"ST*835*0001~"
        b"BPR*I*100.00*C*ACH*CCP*01*999999999*DA*1234*****20240131~"
        b"TRN*1*TRACE1*9000~"
        b"SE*4*0001~GE*1*1~IEA*1*000000001~"
    )

    result = load(remittance, archive_root, name="wrong-kind.edi")
    assert result.outcome is ClaimLoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_CONTAINS_CLAIM


@pytest.mark.invariant
def test_raw_archived_before_parse(archive_root: Path) -> None:
    """PD-9, on the claim side.

    The file that breaks the parser is the one nobody can reproduce, and the
    sender will not resend it because as far as they are concerned they
    already did.
    """
    garbage = b"ISA*this is not a valid interchange at all~~~"
    result = load(garbage, archive_root, name="broken.edi")

    assert result.outcome is ClaimLoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_PARSEABLE
    assert read_archived(result.archived.relative_path, archive_root=archive_root) == garbage

    with session_scope() as session:
        assert counts(session)["raw_file"] == 1
        assert counts(session)["claim"] == 0


# ---------------------------------------------------------------------------
# Idempotency and scoping
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_reprocessing_an_837_is_idempotent(archive_root: Path) -> None:
    """PD-14: the same bytes twice change nothing."""
    payload = file_for(claim("CLM1"), claim("CLM1R1", original="CLM1"))

    first = load(payload, archive_root)
    assert first.outcome is ClaimLoadOutcome.LOADED
    with session_scope() as session:
        before = counts(session)

    second = load(payload, archive_root)
    assert second.outcome is ClaimLoadOutcome.ALREADY_LOADED
    assert second.claims_loaded == 0
    with session_scope() as session:
        assert counts(session) == before


def test_the_same_claim_id_from_a_different_file_is_not_duplicated(
    archive_root: Path,
) -> None:
    """Different bytes, same CLM01. The claim already exists, so nothing is
    added -- without this, a resend under a new envelope doubles the claim."""
    load(file_for(claim("CLM1"), control="000100026"), archive_root, name="one.edi")
    load(file_for(claim("CLM1"), control="000100099"), archive_root, name="two.edi")

    with session_scope() as session:
        totals = counts(session)
        assert totals["claim"] == 1
        assert totals["raw_file"] == 2


def test_two_clients_may_use_the_same_claim_id(archive_root: Path) -> None:
    """PD-20: CLM01 is unique per client, not globally.

    Two practices numbering their claims from 1 is normal, and a global
    uniqueness constraint would make the second one's load fail.
    """
    load(file_for(claim("CLM1")), archive_root, client=CLIENT_A, name="a.edi")
    load(file_for(claim("CLM1"), control="000100027"), archive_root, client=CLIENT_B, name="b.edi")

    with session_scope() as session:
        assert counts(session)["claim"] == 2
        owners = set(session.scalars(select(Claim.client_id)))
        assert owners == {CLIENT_A, CLIENT_B}


def test_a_resubmission_does_not_chain_across_clients(archive_root: Path) -> None:
    """Client A's claim is not an original for client B's resubmission."""
    load(file_for(claim("CLM1")), archive_root, client=CLIENT_A, name="a.edi")
    result = load(
        file_for(claim("CLM1R1", original="CLM1"), control="000100027"),
        archive_root,
        client=CLIENT_B,
        name="b.edi",
    )

    assert result.outcome is ClaimLoadOutcome.QUARANTINED
    assert result.quarantine_assertion == ASSERTION_KNOWN_ORIGINAL


# ---------------------------------------------------------------------------
# What was stored
# ---------------------------------------------------------------------------


def test_the_claim_and_its_lines_are_stored_faithfully(archive_root: Path) -> None:
    detailed = claim(
        "CLM1",
        charge="300.00",
        lines=(
            ClaimServiceLine(
                line_number=1,
                procedure_code="99213",
                charge_amount=Decimal("100.00"),
                modifiers=("59", "25"),
                units=Decimal("1.5"),
                diagnosis_pointers=(1, 2),
                service_date=date(2024, 1, 1),
            ),
            ClaimServiceLine(
                line_number=2,
                procedure_code="J1100",
                charge_amount=Decimal("200.00"),
                diagnosis_pointers=(2,),
            ),
        ),
    )
    load(file_for(detailed), archive_root)

    with session_scope() as session:
        stored = session.scalar(select(Claim))
        assert stored is not None
        assert stored.payer_id == "00590"
        assert stored.billing_npi == "4952654158"
        assert stored.billing_tax_id == "549656931"
        assert stored.subscriber_member_id == "VAZ957419823"
        assert stored.place_of_service == "11"
        assert stored.date_of_service == date(2024, 1, 1)
        assert stored.diagnoses == ["J06.9", "E11.65"]

        lines = session.scalars(select(ClaimLine).order_by(ClaimLine.line_number)).all()
        assert [line.procedure_code for line in lines] == ["99213", "J1100"]
        # DR-05: ordered positional columns, so the order survives storage.
        assert (lines[0].modifier_1, lines[0].modifier_2) == ("59", "25")
        assert (lines[0].modifier_3, lines[0].modifier_4) == (None, None)
        assert lines[0].units == Decimal("1.5")
        assert lines[0].diagnosis_pointers == [1, 2]
        assert lines[1].diagnosis_pointers == [2]


def test_a_loaded_file_is_marked_loaded(archive_root: Path) -> None:
    load(file_for(claim("CLM1")), archive_root)

    with session_scope() as session:
        raw_file = session.scalar(select(RawFile))
        assert raw_file is not None
        assert raw_file.status == str(RawFileStatus.LOADED)
        assert raw_file.quarantine_assertion is None
