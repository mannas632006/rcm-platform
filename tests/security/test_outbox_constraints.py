"""The database refuses an outbox row that breaks IR-06 or IR-07 (M3.7).

Through the application role, which is what the platform runs as.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError

from rcm.domain.claim import Claim
from rcm.ingestion.claim_loader import load_claim_file
from rcm.platform.db import reset_engines, session_scope
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.security

CLIENT = uuid.UUID("7c7c7c7c-0000-4000-8000-00000000007c")


TABLES = "submission_outbox, claim_line, claim, raw_file, payer_alias, payer"


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def claim_id(tmp_path: Path) -> uuid.UUID:
    root = tmp_path / "raw"
    root.mkdir()
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim("CLM-OUTBOX-SEC")),
            client_id=CLIENT,
            original_filename="claims.edi",
            archive_root=root,
        )
    with session_scope() as session:
        return session.scalars(select(Claim.claim_id).where(Claim.client_id == CLIENT)).one()


def _insert(conn: object, claim_id: uuid.UUID, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "claim": claim_id,
        "client": CLIENT,
        "key": "submit-1",
        "status": "PENDING",
        "sent_at": None,
        "via": None,
        "receipt": None,
    }
    values.update(overrides)
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO submission_outbox (outbox_id, client_id, claim_id, idempotency_key, "
            "payload, payload_sha256, status, next_attempt_at, sent_at, sent_via, receipt_id) "
            "VALUES (:id, :client, :claim, :key, 'x', 'h', :status, now(), "
            ":sent_at, :via, :receipt)"
        ),
        values,
    )


@pytest.mark.invariant
def test_one_idempotency_key_is_one_submission(app_engine: Engine, claim_id: uuid.UUID) -> None:
    """IR-07 / EC-41 at the schema level."""
    with app_engine.begin() as conn:
        _insert(conn, claim_id)
    with (
        pytest.raises(IntegrityError, match="uq_outbox_idempotency_key"),
        app_engine.begin() as conn,
    ):
        _insert(conn, claim_id)


def test_a_sent_row_must_record_its_receipt(app_engine: Engine, claim_id: uuid.UUID) -> None:
    """ "Sent" with no receipt is a claim nobody can prove left the building."""
    with (
        pytest.raises(IntegrityError, match="a_sent_row_records_its_receipt"),
        app_engine.begin() as conn,
    ):
        _insert(conn, claim_id, status="SENT")


def test_a_pending_row_has_no_receipt(app_engine: Engine, claim_id: uuid.UUID) -> None:
    with (
        pytest.raises(IntegrityError, match="a_sent_row_records_its_receipt"),
        app_engine.begin() as conn,
    ):
        _insert(conn, claim_id, sent_at="2026-06-01 12:00+00", via="primary", receipt="r-1")
