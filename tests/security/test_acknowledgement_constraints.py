"""The database refuses an acknowledgement that conflates PD-4 (M1.6).

    PD-4: Rejections and denials are distinct outcome types. Never conflated.

`claim_acknowledgement` has no `DENIED` outcome, and that absence is the
control. A loader written in a hurry, or a backfill script, or an operator with
psql, cannot record a denial here -- not because a convention says not to, but
because the CHECK refuses the row.

Every assertion goes through the **application** role, because that is the
identity the platform runs as. A constraint only the owner observes is not a
control.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DataError, IntegrityError

pytestmark = pytest.mark.security

CLIENT = uuid.UUID("cccccccc-0000-4000-8000-00000000000c")
OTHER = uuid.UUID("dddddddd-0000-4000-8000-00000000000d")


@pytest.fixture
def raw_file(owner_engine: Engine) -> Iterator[uuid.UUID]:
    raw_file_id = uuid.uuid4()

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text("TRUNCATE claim_acknowledgement, raw_file RESTART IDENTITY CASCADE"))

    wipe()
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_file (raw_file_id, client_id, content_hash, byte_size, "
                "storage_path, original_filename, transaction_kind, received_at, status) "
                "VALUES (:id, :client, :hash, 1, 'p', 'f.edi', '277', now(), 'ARCHIVED')"
            ),
            {"id": raw_file_id, "client": CLIENT, "hash": uuid.uuid4().hex},
        )
    yield raw_file_id
    wipe()


def _insert(conn: object, raw_file: uuid.UUID, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "client": CLIENT,
        "raw": raw_file,
        "claim": "C1",
        "pcn": "PCNC1",
        "source": "277CA",
        "outcome": "REJECTED",
        "category": "A3",
        "status": "21",
        "description": "Returned unprocessable",
    }
    values.update(overrides)
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO claim_acknowledgement (acknowledgement_id, client_id, raw_file_id, "
            "claim_id, payer_claim_number, source, outcome, category_code, status_code, "
            "description, received_at) "
            "VALUES (:id, :client, :raw, :claim, :pcn, :source, :outcome, :category, "
            ":status, :description, now())"
        ),
        values,
    )


def test_a_well_formed_acknowledgement_is_accepted(app_engine: Engine, raw_file: uuid.UUID) -> None:
    """The baseline. Without it every refusal below could be the row being
    wrong for a reason nobody named."""
    with app_engine.begin() as conn:
        _insert(conn, raw_file)


# ---------------------------------------------------------------------------
# PD-4
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_an_acknowledgement_cannot_be_recorded_as_a_denial(
    app_engine: Engine, raw_file: uuid.UUID
) -> None:
    """PD-4, enforced rather than documented.

    A denial is what an 835 says about a claim that was adjudicated. This
    table holds verdicts from before adjudication, so `DENIED` is not a value
    it has -- and the schema is where that is stated, because a comment does
    not stop an INSERT.
    """
    with pytest.raises(IntegrityError, match="outcome_is_known"), app_engine.begin() as conn:
        _insert(conn, raw_file, outcome="DENIED")


@pytest.mark.parametrize("outcome", ["denied", "PARTIAL", "PENDING", "", "WITHDRAWN"])
def test_only_the_two_outcomes_are_storable(
    app_engine: Engine, raw_file: uuid.UUID, outcome: str
) -> None:
    """Two values and no third. A new outcome needs a migration and the
    thinking that goes with one, not a string a caller invented."""
    with pytest.raises(IntegrityError, match="outcome_is_known"), app_engine.begin() as conn:
        _insert(conn, raw_file, outcome=outcome)


@pytest.mark.parametrize("source", ["835", "837", "271", "", "277ca"])
def test_an_acknowledgement_cannot_come_from_a_transaction_that_carries_none(
    app_engine: Engine, raw_file: uuid.UUID, source: str
) -> None:
    """`835` is the one that matters: an 835 carries denials, and letting it
    be a source here is exactly the conflation PD-4 forbids. The lowercase
    `277ca` is here because a case-insensitive match would make the enum
    decorative."""
    with pytest.raises(IntegrityError, match="source_is_known"), app_engine.begin() as conn:
        _insert(conn, raw_file, source=source)


# ---------------------------------------------------------------------------
# A rejection has to be actionable
# ---------------------------------------------------------------------------


def test_a_rejection_must_name_its_reason(app_engine: Engine, raw_file: uuid.UUID) -> None:
    """The whole value of a rejection over a denial is that it can be fixed
    and resent. A rejection with no category says only "no", which puts the
    claim in a queue nobody can work."""
    with (
        pytest.raises(IntegrityError, match="a_rejection_names_its_reason"),
        app_engine.begin() as conn,
    ):
        _insert(conn, raw_file, category_code="", category="")


def test_a_999_rejection_needs_no_category(app_engine: Engine, raw_file: uuid.UUID) -> None:
    """The deliberate exemption: a 999 carries its reason in the IK3/IK4
    error detail, so requiring a status category would make every 999
    rejection unstorable."""
    with app_engine.begin() as conn:
        _insert(conn, raw_file, source="999", category="", status="", description="CLM@23:7")


def test_an_acceptance_needs_no_category(app_engine: Engine, raw_file: uuid.UUID) -> None:
    """The constraint is on rejections only. An acceptance is not actionable
    and has nothing to explain."""
    with app_engine.begin() as conn:
        _insert(conn, raw_file, outcome="ACCEPTED", category="")


def test_an_acknowledgement_must_name_a_claim(app_engine: Engine, raw_file: uuid.UUID) -> None:
    """A verdict attributed to no claim cannot be acted on and cannot be
    counted, so it is worse than absent -- it inflates a total."""
    with (
        pytest.raises(IntegrityError, match="acknowledgement_names_its_claim"),
        app_engine.begin() as conn,
    ):
        _insert(conn, raw_file, claim="")


# ---------------------------------------------------------------------------
# Provenance and scoping
# ---------------------------------------------------------------------------


def test_an_acknowledgement_must_come_from_an_archived_file(
    app_engine: Engine, raw_file: uuid.UUID
) -> None:
    """PD-9/PD-10: every row traces to bytes on disk, so the table can be
    rebuilt from RAW and no verdict exists that no file explains."""
    del raw_file
    with pytest.raises(IntegrityError, match="raw_file"), app_engine.begin() as conn:
        _insert(conn, uuid.uuid4())


def test_the_same_verdict_cannot_be_stored_twice_from_one_file(
    app_engine: Engine, raw_file: uuid.UUID
) -> None:
    """PD-14 at the row level. A loader that runs twice on one file must not
    double a count, and this is the guard for one that gets past the
    content-hash check."""
    with pytest.raises(IntegrityError, match="uq_acknowledgement_fact"), app_engine.begin() as conn:
        _insert(conn, raw_file)
        _insert(conn, raw_file)


def test_an_outcome_longer_than_the_column_is_refused_too(
    app_engine: Engine, raw_file: uuid.UUID
) -> None:
    """Belt and braces, and worth naming separately: a long value is refused
    by the column width before the CHECK ever sees it, so the two controls
    cover different inputs and neither makes the other redundant."""
    with pytest.raises(DataError), app_engine.begin() as conn:
        _insert(conn, raw_file, outcome="ACCEPTED_WITH_ERRORS")


def test_two_clients_may_be_given_the_same_claim_id(
    owner_engine: Engine, app_engine: Engine, raw_file: uuid.UUID
) -> None:
    """PD-20. Claim control numbers are the submitter's, not globally unique,
    so two clients can be sent the same one and neither load may fail because
    of the other.

    The two verdicts arrive in two files, which is the only way they can: a
    `raw_file` belongs to exactly one client, so `uq_acknowledgement_fact`
    keying on `raw_file_id` already scopes the fact to a client without
    naming `client_id`. One file carrying two clients' verdicts is not a case
    to permit, it is a thing that cannot be archived in the first place.
    """
    other_file = uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_file (raw_file_id, client_id, content_hash, byte_size, "
                "storage_path, original_filename, transaction_kind, received_at, status) "
                "VALUES (:id, :client, :hash, 1, 'p', 'g.edi', '277', now(), 'ARCHIVED')"
            ),
            {"id": other_file, "client": OTHER, "hash": uuid.uuid4().hex},
        )

    with app_engine.begin() as conn:
        _insert(conn, raw_file, claim="SHARED")
        _insert(conn, other_file, client=OTHER, claim="SHARED")
