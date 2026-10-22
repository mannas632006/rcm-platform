"""The database refuses chains that do not make sense (M1.9).

The loader resolves the resubmission chain correctly today. These constraints
are for the loader that does not -- the one written in a hurry against a
deadline, by someone who has not read BR-DATA-03 and for whom "just insert it
as a new claim" is the obvious way to make an orphaned resubmission load.

Every assertion runs through the **application** role, because that is the
identity the platform runs as. A constraint only the owner observes is not a
control.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.security

CLIENT = uuid.UUID("cccccccc-0000-4000-8000-00000000000c")

_INSERT_CLAIM = text(
    "INSERT INTO claim (claim_id, client_id, raw_file_id, submitter_claim_id, "
    "original_claim_id, root_claim_id, submission_attempt, payer_id, billing_npi, "
    "subscriber_member_id, total_charge) "
    "VALUES (:id, :client, :raw, :submitter, :original, :root, :attempt, 'P-CLAIM', "
    "'1234567893', 'MEM1', 100.00)"
)

_INSERT_LINE = text(
    "INSERT INTO claim_line (claim_line_id, client_id, claim_id, line_number, "
    "procedure_code, units, charge_amount, diagnosis_pointers) "
    "VALUES (:id, :client, :claim, :line_number, :procedure, :units, :charge, :pointers)"
)


@pytest.fixture
def seeded(owner_engine: Engine, app_engine: Engine) -> Iterator[dict[str, uuid.UUID]]:
    """A payer, a raw file and one original claim to chain from."""
    del app_engine  # requested so an unreachable database fails before setup
    ids = {"raw_file": uuid.uuid4(), "original": uuid.uuid4()}

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE claim_line, claim, remittance_adjustment, "
                    "remittance_service_line, claim_payment, provider_adjustment, "
                    "remittance, raw_file, payer_alias, payer RESTART IDENTITY CASCADE"
                )
            )

    wipe()
    with owner_engine.begin() as conn:
        conn.execute(text("INSERT INTO payer (payer_id, name) VALUES ('P-CLAIM', 'Test Payer')"))
        conn.execute(
            text(
                "INSERT INTO raw_file (raw_file_id, client_id, content_hash, byte_size, "
                "storage_path, original_filename, transaction_kind, received_at, status) "
                "VALUES (:id, :client, :hash, 1, 'p', 'f.edi', '837', now(), 'LOADED')"
            ),
            {"id": ids["raw_file"], "client": CLIENT, "hash": uuid.uuid4().hex},
        )
        conn.execute(
            _INSERT_CLAIM,
            {
                "id": ids["original"],
                "client": CLIENT,
                "raw": ids["raw_file"],
                "submitter": "CLM1",
                "original": None,
                "root": ids["original"],
                "attempt": 1,
            },
        )

    yield ids
    wipe()


def _claim_values(seeded: dict[str, uuid.UUID], **overrides: object) -> dict[str, object]:
    claim_id = uuid.uuid4()
    values: dict[str, object] = {
        "id": claim_id,
        "client": CLIENT,
        "raw": seeded["raw_file"],
        "submitter": f"CLM-{uuid.uuid4().hex[:8]}",
        "original": seeded["original"],
        "root": seeded["original"],
        "attempt": 2,
    }
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# BR-DATA-03: the two representations of the chain must agree
# ---------------------------------------------------------------------------


def test_a_well_formed_resubmission_is_accepted(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """The control has to let the right thing through, or it proves nothing."""
    with app_engine.begin() as conn:
        conn.execute(_INSERT_CLAIM, _claim_values(seeded))


def test_a_claim_cannot_replace_itself(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    """A one-row cycle, which every chain walk follows forever."""
    claim_id = uuid.uuid4()
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(
            _INSERT_CLAIM,
            _claim_values(seeded, id=claim_id, original=claim_id, root=claim_id),
        )


def test_an_original_claim_must_be_its_own_root(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """No REF*F8, so it starts a chain -- and a claim that starts a chain and
    points its root elsewhere makes the stored root untrustworthy."""
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(
            _INSERT_CLAIM,
            _claim_values(seeded, original=None, root=seeded["original"], attempt=1),
        )


def test_a_resubmission_cannot_be_its_own_root(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """This is the shape that inflates the claim count.

    A resubmission rooted at itself counts as a separate claim in exactly the
    query that is supposed to collapse it, which is the distortion BR-DATA-03
    exists to prevent.
    """
    claim_id = uuid.uuid4()
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(_INSERT_CLAIM, _claim_values(seeded, id=claim_id, root=claim_id))


def test_an_original_claim_is_attempt_one(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(_INSERT_CLAIM, _claim_values(seeded, original=None, attempt=2))


def test_a_resubmission_is_not_attempt_one(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(_INSERT_CLAIM, _claim_values(seeded, attempt=1))


def test_a_chain_cannot_point_at_a_claim_that_does_not_exist(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """The orphan case, refused by the database as well as by IR-05."""
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(_INSERT_CLAIM, _claim_values(seeded, original=uuid.uuid4()))


def test_the_same_claim_id_cannot_be_loaded_twice_for_one_client(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """PD-14 at the table: a resend is recognised, not duplicated."""
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(
            _INSERT_CLAIM,
            _claim_values(seeded, submitter="CLM1", original=None, root=None, attempt=1),
        )


def test_two_clients_may_use_the_same_claim_id(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """Uniqueness is per client. Two practices numbering claims from 1 is
    normal, and a global constraint would break the second one."""
    other_client = uuid.UUID("dddddddd-0000-4000-8000-00000000000d")
    claim_id = uuid.uuid4()
    with app_engine.begin() as conn:
        conn.execute(
            _INSERT_CLAIM,
            _claim_values(
                seeded,
                id=claim_id,
                client=other_client,
                submitter="CLM1",
                original=None,
                root=claim_id,
                attempt=1,
            ),
        )


# ---------------------------------------------------------------------------
# BR-M6-01 and the service line
# ---------------------------------------------------------------------------


def _line_values(claim_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "client": CLIENT,
        "claim": claim_id,
        "line_number": 1,
        "procedure": "99213",
        "units": 1,
        "charge": 100,
        "pointers": [1],
    }
    values.update(overrides)
    return values


def test_a_well_formed_line_is_accepted(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    with app_engine.begin() as conn:
        conn.execute(_INSERT_LINE, _line_values(seeded["original"]))


@pytest.mark.invariant
def test_a_line_must_carry_a_diagnosis_pointer(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """BR-M6-01 at the table, not only in the loader that happens to write it."""
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(_INSERT_LINE, _line_values(seeded["original"], pointers=[]))


def test_a_line_cannot_have_zero_units(app_engine: Engine, seeded: dict[str, uuid.UUID]) -> None:
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(_INSERT_LINE, _line_values(seeded["original"], units=0))


def test_a_line_cannot_have_an_empty_procedure_code(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(_INSERT_LINE, _line_values(seeded["original"], procedure=""))


def test_one_claim_cannot_have_two_lines_with_the_same_number(
    app_engine: Engine, seeded: dict[str, uuid.UUID]
) -> None:
    """Line numbers are how the 835 points back at what it adjudicated."""
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(_INSERT_LINE, _line_values(seeded["original"]))
        conn.execute(_INSERT_LINE, _line_values(seeded["original"]))
