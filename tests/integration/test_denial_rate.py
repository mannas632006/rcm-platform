"""The denial rate, counted per claim rather than per submission (M1.9).

    BR-DATA-03 (MUST): Resubmissions MUST be chained to the original claim,
    not stored as independent claims. Failure to do this inflates volume and
    distorts the denial rate.

`test_denial_rate_excludes_resubmissions` is the invariant CLAUDE.md section
7.2 names, and it is the reason the chaining work exists. Everything else in
M1.9 is machinery for getting this number right.

The tests are built from small, explicit scenarios rather than the corpus,
because the point is to be able to count the expected answer by hand. The
corpus figure is asserted separately in `test_claim_corpus_load.py`, where the
numbers come out as 15.17% against a per-submission 17.37%.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from rcm.analytics.denials import denial_rate
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.remittance_loader import load_remittance_file
from rcm.platform.db import reset_engines, session_scope
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.integration

CLIENT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
CLIENT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")


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


def remittance_for(
    *outcomes: tuple[str, str], control: str = "0001", charge: str = "200.00"
) -> bytes:
    """An 835 paying, partly paying, or denying each named claim.

    `outcomes` is `(claim_id, "paid" | "partial" | "denied")`:

        paid      paid in full, no adjustment
        partial   paid less than charged, the difference written off under
                  CO-45 -- an *adjustment*, which is what almost every real
                  line carries
        denied    paid nothing, with CO-16 as the reason

    The distinction between `partial` and `denied` is the one that matters.
    DOC-02's glossary defines a denial as *"Payer refusal to pay a line or
    claim, with a reason code"*, so both halves are required: a reason code on
    a line that was still paid is an adjustment, not a refusal.

    An earlier version of this helper had no `partial` case and treated any
    outcome other than `denied` as paid in full, which emits no CAS segment at
    all. The test asserting that a contractual adjustment is not a denial
    therefore passed without ever creating one -- it was checking that a line
    with no adjustment is not a denial, which nothing was in danger of getting
    wrong.

    Each line satisfies IR-02 (SVC02 == SVC03 + sum(CAS)) and the file
    satisfies BR-M9-01, so the remittance loader accepts it.
    """
    body: list[str] = []
    total = Decimal("0.00")
    charged = Decimal(charge)
    write_off = Decimal("50.00")

    for claim_id, outcome in outcomes:
        if outcome == "denied":
            paid, adjustment, group_reason, status = charged * 0, charged, "CO*16", "4"
        elif outcome == "partial":
            paid, adjustment, group_reason, status = (
                charged - write_off,
                write_off,
                "CO*45",
                "1",
            )
        else:
            paid, adjustment, group_reason, status = charged, Decimal("0.00"), "", "1"
        total += paid

        body.extend(
            [
                f"CLP*{claim_id}*{status}*{charge}*{paid}**MB*PCN{claim_id}*11~",
                "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
                f"SVC*HC:99203*{charge}*{paid}**1~",
            ]
        )
        if adjustment:
            body.append(f"CAS*{group_reason}*{adjustment}~")
        body.append(f"AMT*B6*{paid}~")

    header = [
        f"ST*835*{control}~",
        f"BPR*I*{total}*C*ACH*CCP*01*999999999*DA*1234*****20240131~",
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
        isa()
        + "GS*HP*PAYER0001*PAYEE0001*20240131*0000*1*X*005010X221A1~"
        + "".join(header)
        + "GE*1*1~IEA*1*000000001~"
    ).encode()


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


def load_claims(payload: bytes, root: Path, *, client: uuid.UUID = CLIENT_A, name: str) -> None:
    with session_scope() as session:
        load_claim_file(
            session, payload, client_id=client, original_filename=name, archive_root=root
        )


def load_remittance(payload: bytes, root: Path, *, client: uuid.UUID = CLIENT_A, name: str) -> None:
    with session_scope() as session:
        load_remittance_file(
            session, payload, client_id=client, original_filename=name, archive_root=root
        )


def rate_for(client: uuid.UUID = CLIENT_A):
    with session_scope() as session:
        return denial_rate(session, client_id=client)


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_denial_rate_excludes_resubmissions(archive_root: Path) -> None:
    """One claim, denied, resubmitted, denied again. One denial, one claim.

    Counted per submission this scenario reads as four submissions, two of
    them denied: a 50% denial rate on two claims that are really one denied
    claim and one paid one. The right answer is 50% as well -- which is why
    the numbers below are chosen so the two *disagree*: three claims, one of
    which was resubmitted twice.
    """
    # CLM1: denied, resubmitted, denied again, resubmitted, finally paid.
    load_claims(file_for(claim("CLM1")), archive_root, name="c1.edi")
    load_claims(file_for(claim("CLM1R1", original="CLM1")), archive_root, name="c1r1.edi")
    load_claims(file_for(claim("CLM1R2", original="CLM1R1")), archive_root, name="c1r2.edi")
    # CLM2 and CLM3: submitted once each, both paid.
    load_claims(file_for(claim("CLM2")), archive_root, name="c2.edi")
    load_claims(file_for(claim("CLM3")), archive_root, name="c3.edi")

    load_remittance(
        remittance_for(
            ("CLM1", "denied"),
            ("CLM1R1", "denied"),
            ("CLM1R2", "paid"),
            ("CLM2", "paid"),
            ("CLM3", "paid"),
        ),
        archive_root,
        name="r.edi",
    )

    result = rate_for()

    # Five submissions, two of which carried a denial.
    assert result.submissions == 5
    assert result.denied_submissions == 2
    assert result.per_submission == Decimal("40.00")

    # But three claims, one of which was denied.
    assert result.claims == 3
    assert result.denied_claims == 1
    assert result.rate == Decimal("33.33")

    # Counting submissions overstates by nearly seven points on five claims.
    assert result.inflation == Decimal("6.67")


def test_a_claim_denied_only_on_its_resubmission_still_counts_once(
    archive_root: Path,
) -> None:
    """The denial can land anywhere in the chain and the claim is denied once."""
    load_claims(file_for(claim("CLM1")), archive_root, name="a.edi")
    load_claims(file_for(claim("CLM1R1", original="CLM1")), archive_root, name="b.edi")
    load_remittance(
        remittance_for(("CLM1", "paid"), ("CLM1R1", "denied")), archive_root, name="r.edi"
    )

    result = rate_for()
    assert result.claims == 1
    assert result.denied_claims == 1
    assert result.rate == Decimal("100.00")


def test_a_claim_denied_at_every_attempt_counts_once(archive_root: Path) -> None:
    load_claims(file_for(claim("CLM1")), archive_root, name="a.edi")
    load_claims(file_for(claim("CLM1R1", original="CLM1")), archive_root, name="b.edi")
    load_claims(file_for(claim("CLM2")), archive_root, name="c.edi")
    load_remittance(
        remittance_for(("CLM1", "denied"), ("CLM1R1", "denied"), ("CLM2", "paid")),
        archive_root,
        name="r.edi",
    )

    result = rate_for()
    assert (result.denied_submissions, result.submissions) == (2, 3)
    assert (result.denied_claims, result.claims) == (1, 2)
    assert result.rate == Decimal("50.00")
    assert result.per_submission == Decimal("66.67")


# ---------------------------------------------------------------------------
# What counts as a denial
# ---------------------------------------------------------------------------


def test_a_paid_claim_is_not_denied(archive_root: Path) -> None:
    load_claims(file_for(claim("CLM1")), archive_root, name="a.edi")
    load_remittance(remittance_for(("CLM1", "paid")), archive_root, name="r.edi")

    result = rate_for()
    assert result.denied_claims == 0
    assert result.rate == Decimal("0.00")


def test_a_contractual_adjustment_on_a_paid_line_is_not_a_denial(
    archive_root: Path,
) -> None:
    """A reason code on a line that was still paid is an adjustment.

    Treating every CAS segment as a denial would report a denial rate near
    100%, because almost every line carries a contractual write-off.
    """
    load_claims(file_for(claim("CLM1", charge="200.00")), archive_root, name="a.edi")
    # Charged 200, paid 150, 50 written off under CO-45: a reason code on a
    # line that was still paid. The first version of this test used a fully
    # paid line, which carries no CAS segment at all -- so it passed without
    # ever creating the situation it describes.
    load_remittance(remittance_for(("CLM1", "partial")), archive_root, name="r.edi")

    result = rate_for()
    assert result.denied_claims == 0
    assert result.claims == 1
    assert result.rate == Decimal("0.00")


def test_a_claim_with_no_remittance_is_not_counted(archive_root: Path) -> None:
    """A claim still in flight has been neither denied nor paid.

    Including unadjudicated claims in the denominator makes the denial rate
    fall every time volume grows, which looks like an improvement.
    """
    load_claims(file_for(claim("CLM1")), archive_root, name="a.edi")
    load_claims(file_for(claim("CLM2")), archive_root, name="b.edi")
    load_remittance(remittance_for(("CLM1", "denied")), archive_root, name="r.edi")

    result = rate_for()
    assert result.claims == 1
    assert result.denied_claims == 1
    assert result.rate == Decimal("100.00")


def test_an_empty_period_reports_zero_rather_than_raising(archive_root: Path) -> None:
    """A quiet week is a real answer, and a dashboard that throws gets routed
    around rather than fixed."""
    del archive_root
    result = rate_for()
    assert result.claims == 0
    assert result.rate == Decimal("0.00")
    assert result.per_submission == Decimal("0.00")


# ---------------------------------------------------------------------------
# PD-20
# ---------------------------------------------------------------------------


def test_the_denial_rate_is_scoped_to_one_client(archive_root: Path) -> None:
    """Client B's denials must not reach client A's number.

    Both clients deliberately use the claim id `CLM1`. Two practices numbering
    their claims from 1 is ordinary, and it is also the only arrangement in
    which this control can be *shown* to work: with distinct claim ids, the
    join to `claim_payment` happens to exclude the other client's rows anyway,
    so dropping the client filter on `claim` changes nothing and the test
    passes either way. With the ids shared, A's query picks up B's claim row
    the moment the filter goes.
    """
    load_claims(
        file_for(claim("CLM1", charge="200.00")), archive_root, client=CLIENT_A, name="a.edi"
    )
    load_remittance(remittance_for(("CLM1", "paid")), archive_root, client=CLIENT_A, name="ra.edi")

    load_claims(
        file_for(claim("CLM1", charge="200.00"), control="000100099"),
        archive_root,
        client=CLIENT_B,
        name="b.edi",
    )
    load_remittance(
        remittance_for(("CLM1", "denied"), control="0002"),
        archive_root,
        client=CLIENT_B,
        name="rb.edi",
    )

    for_a = rate_for(CLIENT_A)
    for_b = rate_for(CLIENT_B)

    assert (for_a.claims, for_a.denied_claims, for_a.rate) == (1, 0, Decimal("0.00"))
    assert (for_b.claims, for_b.denied_claims, for_b.rate) == (1, 1, Decimal("100.00"))
