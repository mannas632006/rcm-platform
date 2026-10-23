"""The database refuses what the prime directives forbid (M1.8).

A test can be deleted and a loader can be rewritten in a hurry. A `NOT NULL`
cannot be worked around by code that did not think about it, which is why
PD-3's group code and PD-8's payer key are expressed as constraints and not
only as conventions.

Every assertion here goes through the **application** role, because that is the
identity the platform actually runs as. A constraint that only the owner
observes is not a control.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DatabaseError, IntegrityError

pytestmark = pytest.mark.security

CLIENT = uuid.UUID("cccccccc-0000-4000-8000-00000000000c")


@pytest.fixture
def seeded(owner_engine: Engine, app_engine: Engine) -> Iterator[dict[str, uuid.UUID]]:
    """A payer, raw file, remittance, claim and line to hang adjustments from.

    The tables are truncated at setup rather than cleaned up at teardown. A
    teardown that deletes in dependency order is correct right up until one
    test leaves the database in a shape it did not expect, and then every
    subsequent test inherits the mess -- which is what the first version of
    this fixture did, accumulating 27 stray rows before anyone noticed.
    """
    del app_engine  # requested so an unreachable database fails before setup
    ids = {
        "raw_file": uuid.uuid4(),
        "remittance": uuid.uuid4(),
        "claim": uuid.uuid4(),
        "line": uuid.uuid4(),
    }

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE remittance_adjustment, remittance_service_line, "
                    "claim_payment, provider_adjustment, remittance, raw_file, "
                    "payer_alias, payer RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO payer (payer_id, name) VALUES ('P-CONSTRAINT', 'Test Payer')")
        )
        conn.execute(
            text(
                "INSERT INTO raw_file (raw_file_id, client_id, content_hash, byte_size, "
                "storage_path, original_filename, transaction_kind, received_at, status) "
                "VALUES (:id, :client, :hash, 1, 'p', 'f.edi', '835', now(), 'ARCHIVED')"
            ),
            {"id": ids["raw_file"], "client": CLIENT, "hash": uuid.uuid4().hex},
        )
        conn.execute(
            text(
                "INSERT INTO remittance (remittance_id, client_id, raw_file_id, payer_id, "
                "payment_amount, claim_payment_total, provider_adjustment_total) "
                "VALUES (:id, :client, :raw, 'P-CONSTRAINT', 0, 0, 0)"
            ),
            {"id": ids["remittance"], "client": CLIENT, "raw": ids["raw_file"]},
        )
        conn.execute(
            text(
                "INSERT INTO claim_payment (claim_payment_id, client_id, remittance_id, "
                "claim_id, status_code, total_charge, total_paid, patient_responsibility) "
                "VALUES (:id, :client, :rem, 'C1', '1', 0, 0, 0)"
            ),
            {"id": ids["claim"], "client": CLIENT, "rem": ids["remittance"]},
        )
        conn.execute(
            text(
                "INSERT INTO remittance_service_line (service_line_id, client_id, "
                "claim_payment_id, line_number, procedure_code, charge_amount, paid_amount) "
                "VALUES (:id, :client, :claim, 1, '99213', 0, 0)"
            ),
            {"id": ids["line"], "client": CLIENT, "claim": ids["claim"]},
        )

    yield ids
    wipe()


def _insert_adjustment(conn: object, seeded: dict[str, uuid.UUID], **overrides: object) -> None:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "client": CLIENT,
        "claim": seeded["claim"],
        "line": seeded["line"],
        "group": "CO",
        "reason": "45",
        "amount": 10,
        "claim_level": False,
    }
    values.update(overrides)
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO remittance_adjustment (adjustment_id, client_id, claim_payment_id, "
            "service_line_id, cas_group_code, reason_code, amount, claim_level, sequence) "
            "VALUES (:id, :client, :claim, :line, :group, :reason, :amount, :claim_level, 1)"
        ),
        values,
    )


# ---------------------------------------------------------------------------
# PD-3
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_cas_group_code_required(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """The database refuses a reason code with no group.

    CO-45 is a contractual write-off the patient may never be billed for.
    PR-45 is an amount they owe. A row without the group is not incomplete, it
    is ambiguous between two opposite outcomes.
    """
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        _insert_adjustment(conn, seeded, group=None)


def test_an_unknown_cas_group_is_refused(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """Code list 1033 is closed. A sixth value means a file we do not
    understand, not a meaning to invent."""
    for invalid in ("ZZ", "co", "C", ""):
        with pytest.raises(DatabaseError), app_engine.begin() as conn:
            _insert_adjustment(conn, seeded, group=invalid)


def test_an_empty_reason_code_is_refused(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    with pytest.raises(DatabaseError), app_engine.begin() as conn:
        _insert_adjustment(conn, seeded, reason="")


def test_every_valid_group_is_accepted(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """The constraint must not be so tight that it rejects real files."""
    with app_engine.begin() as conn:
        for group in ("CO", "PR", "OA", "PI", "CR"):
            _insert_adjustment(conn, seeded, group=group)


# ---------------------------------------------------------------------------
# PD-6
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_multi_cas_adjustments_not_merged(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """The same reason twice on one line is storable.

    This is the constraint that is *absent* on purpose. A unique index on
    (service_line_id, reason_code) looks tidy and would force a loader to
    merge two legitimate adjustments into one -- the collapsing PD-6 exists to
    prevent.
    """
    with app_engine.begin() as conn:
        _insert_adjustment(conn, seeded, reason="45", amount=10)
        _insert_adjustment(conn, seeded, reason="45", amount=25)

        stored = conn.execute(
            text(
                "SELECT count(*) FROM remittance_adjustment "
                "WHERE service_line_id = :line AND reason_code = '45'"
            ),
            {"line": seeded["line"]},
        ).scalar_one()
    assert stored == 2


def test_a_claim_level_adjustment_cannot_also_name_a_service_line(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """Claim-level and line-level adjustments are different money.

    A row that is both is how the same amount gets counted twice.
    """
    with pytest.raises(DatabaseError), app_engine.begin() as conn:
        _insert_adjustment(conn, seeded, claim_level=True, line=seeded["line"])


def test_a_line_level_adjustment_must_name_its_service_line(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(DatabaseError), app_engine.begin() as conn:
        _insert_adjustment(conn, seeded, claim_level=False, line=None)


# ---------------------------------------------------------------------------
# PD-8
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_payer_identity_is_not_keyed_on_name(app_engine: Engine) -> None:
    """`payer.name` carries no uniqueness constraint, and must not.

    Two payers may legitimately share a display name; the same payer arrives
    under several. A unique index here would be PD-8 violated in the schema,
    so its absence is asserted rather than assumed.
    """
    with app_engine.connect() as conn:
        unique_columns = (
            conn.execute(
                text(
                    """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_class t ON t.oid = i.indrelid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
                WHERE t.relname = 'payer' AND i.indisunique
                """
                )
            )
            .scalars()
            .all()
        )

    assert "name" not in unique_columns, "payer.name is unique -- PD-8 violated in the schema"
    assert "payer_id" in unique_columns, "payer_id is not the key"


def test_a_remittance_cannot_reference_a_payer_that_does_not_exist(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """The foreign key is what keeps payer_id an identifier rather than a label."""
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO remittance (remittance_id, client_id, raw_file_id, payer_id, "
                "payment_amount, trace_number, transaction_control_number, payee_npi, "
                "claim_payment_total, provider_adjustment_total) "
                "VALUES (:id, :client, :raw, 'NO-SUCH-PAYER', 0, 't', 'c2', 'n', 0, 0)"
            ),
            {"id": uuid.uuid4(), "client": CLIENT, "raw": seeded["raw_file"]},
        )


# ---------------------------------------------------------------------------
# DR-10
# ---------------------------------------------------------------------------


def test_a_quarantined_file_must_name_its_assertion(app_engine: Engine) -> None:
    """ "Quarantined, reason unknown" is not an operator-actionable state.

    It is how a quarantine queue becomes a place files go to die, so the
    database refuses it.
    """
    with pytest.raises(DatabaseError), app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_file (raw_file_id, client_id, content_hash, byte_size, "
                "storage_path, original_filename, transaction_kind, received_at, status) "
                "VALUES (:id, :client, :hash, 1, 'p', 'f.edi', '835', now(), 'QUARANTINED')"
            ),
            {"id": uuid.uuid4(), "client": CLIENT, "hash": uuid.uuid4().hex},
        )


def test_an_unknown_raw_file_status_is_refused(app_engine: Engine) -> None:
    """There is deliberately no PARTIALLY_LOADED state. Its absence is the point."""
    with pytest.raises(DatabaseError), app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_file (raw_file_id, client_id, content_hash, byte_size, "
                "storage_path, original_filename, transaction_kind, received_at, status) "
                "VALUES (:id, :client, :hash, 1, 'p', 'f.edi', '835', now(), "
                "'PARTIALLY_LOADED')"
            ),
            {"id": uuid.uuid4(), "client": CLIENT, "hash": uuid.uuid4().hex},
        )


def test_the_same_file_cannot_be_archived_twice(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """Idempotency rests on this uniqueness, not on the loader remembering."""
    with app_engine.connect() as conn:
        existing = conn.execute(
            text("SELECT content_hash FROM raw_file WHERE raw_file_id = :id"),
            {"id": seeded["raw_file"]},
        ).scalar_one()

    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_file (raw_file_id, client_id, content_hash, byte_size, "
                "storage_path, original_filename, transaction_kind, received_at, status) "
                "VALUES (:id, :client, :hash, 1, 'p2', 'again.edi', '835', now(), 'ARCHIVED')"
            ),
            {"id": uuid.uuid4(), "client": CLIENT, "hash": existing},
        )


# ---------------------------------------------------------------------------
# BR-M9-04: reversals (M1.10)
# ---------------------------------------------------------------------------


def _insert_posting(conn: object, seeded: dict[str, uuid.UUID], **overrides: object) -> uuid.UUID:
    posting_id = uuid.uuid4()
    values: dict[str, object] = {
        "id": posting_id,
        "client": CLIENT,
        "rem": seeded["remittance"],
        "claim_id": f"C-{uuid.uuid4().hex[:8]}",
        "is_reversal": False,
        "reverses": None,
    }
    values.update(overrides)
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO claim_payment (claim_payment_id, client_id, remittance_id, claim_id, "
            "status_code, total_charge, total_paid, patient_responsibility, is_reversal, "
            "reverses_claim_payment_id) "
            "VALUES (:id, :client, :rem, :claim_id, '1', 0, 0, 0, :is_reversal, :reverses)"
        ),
        values,
    )
    return posting_id


def test_a_reversal_may_point_at_the_posting_it_reverses(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """The control has to let the right thing through, or it proves nothing."""
    with app_engine.begin() as conn:
        original = _insert_posting(conn, seeded)
        _insert_posting(conn, seeded, is_reversal=True, reverses=original)


def test_a_posting_cannot_reverse_itself(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """A one-row cycle: the posting that undoes itself."""
    posting_id = uuid.uuid4()
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        _insert_posting(conn, seeded, id=posting_id, is_reversal=True, reverses=posting_id)


def test_only_a_reversal_may_reverse_a_posting(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """An ordinary payment linked to an earlier one would quietly remove that
    earlier payment from every "what did the payer actually pay" total."""
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        original = _insert_posting(conn, seeded)
        _insert_posting(conn, seeded, is_reversal=False, reverses=original)


def test_a_reversal_cannot_point_at_a_posting_that_does_not_exist(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        _insert_posting(conn, seeded, is_reversal=True, reverses=uuid.uuid4())


def test_a_recoupment_cannot_point_at_a_posting_that_does_not_exist(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO provider_adjustment (provider_adjustment_id, client_id, "
                "remittance_id, reason_code, reference, amount, recoups_claim_payment_id) "
                "VALUES (:id, :client, :rem, 'WO', 'PCN1', 10.00, :recoups)"
            ),
            {
                "id": uuid.uuid4(),
                "client": CLIENT,
                "rem": seeded["remittance"],
                "recoups": uuid.uuid4(),
            },
        )
