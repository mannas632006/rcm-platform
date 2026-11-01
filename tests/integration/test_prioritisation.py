"""Prioritising real work items against real claims (M3.6).

PD-1: Every decision writes a decision_record with logic version,
inputs, output.
PD-15: Timely-filing deadline overrides all other prioritisation.
FR-WORK-06 (SHOULD): The queue exposes why an item ranks where it does.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from rcm.decision.prioritisation import LOGIC_VERSION
from rcm.domain.claim import Claim
from rcm.ingestion.claim_loader import load_claim_file
from rcm.platform.audit import DecisionRecord
from rcm.platform.config import get_settings
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ResourceNotFoundError
from rcm.work import priority
from rcm.work.exceptions import RecordRef, raise_exception
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.integration

CLIENT = uuid.UUID("6a6a6a6a-0000-4000-8000-00000000006a")
OTHER_CLIENT = uuid.UUID("6b6b6b6b-0000-4000-8000-00000000006b")
#: The fixture claims are for 2024-01-01; with a 365-day term the deadline
#: is 2024-12-31.
FAR = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)  # 213 days left
NEAR = datetime(2024, 12, 25, 12, 0, tzinfo=UTC)  # 6 days left

TABLES = (
    "automation_suspension, work_item, claim_state_transition, claim_acknowledgement, "
    "claim_line, claim, remittance_adjustment, remittance_service_line, claim_payment, "
    "provider_adjustment, remittance, raw_file, payer_alias, payer, payer_match_candidate"
)


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
def archive_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    root.mkdir()
    return root


def load(archive_root: Path, *claim_ids: str, charge: str = "500.00") -> list[uuid.UUID]:
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(*(claim(c, charge=charge) for c in claim_ids)),
            client_id=CLIENT,
            original_filename="claims.edi",
            archive_root=archive_root,
        )
    with session_scope() as session:
        return list(
            session.scalars(
                select(Claim.claim_id)
                .where(Claim.client_id == CLIENT)
                .order_by(Claim.submitter_claim_id)
            ).all()
        )


def set_filing_term(owner_engine: Engine, days: int | None) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE payer SET timely_filing_days = :d"), {"d": days})


def denial_for(claim_id: uuid.UUID) -> uuid.UUID:
    with session_scope() as session:
        item = raise_exception(
            session,
            client_id=CLIENT,
            kind=WorkItemKind.DENIAL,
            problem="CO-50 medical necessity denial on line 1",
            record=RecordRef("claim", claim_id),
            source_key=f"denial:{claim_id}",
            now=FAR,
            claim_id=claim_id,
        )
        return item.work_item_id


def run(item_id: uuid.UUID, now: datetime, client_id: uuid.UUID = CLIENT) -> None:
    with session_scope() as session:
        priority.prioritise_item(session, work_item_id=item_id, client_id=client_id, now=now)


def get(item_id: uuid.UUID) -> WorkItem:
    with session_scope() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        session.expunge(item)
        return item


# ---------------------------------------------------------------------------
# PD-15
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_claim_near_its_deadline_is_pinned(archive_root: Path, owner_engine: Engine) -> None:
    (claim_id,) = load(archive_root, "CLM-NEAR")
    set_filing_term(owner_engine, 365)
    item_id = denial_for(claim_id)

    run(item_id, NEAR)

    item = get(item_id)
    assert (item.deadline_pinned, item.priority_score) == (True, 100)
    assert "6 days" in item.priority_reason


def test_a_claim_far_from_its_deadline_is_scored(archive_root: Path, owner_engine: Engine) -> None:
    (claim_id,) = load(archive_root, "CLM-FAR")
    set_filing_term(owner_engine, 365)
    item_id = denial_for(claim_id)

    run(item_id, FAR)

    item = get(item_id)
    assert item.deadline_pinned is False
    assert 0 <= item.priority_score <= 99
    assert item.priority_components is not None
    assert item.priority_components["expected_value"] == "250.00"  # 500 x 0.50
    assert item.priority_logic_version == LOGIC_VERSION


def test_the_escalation_threshold_comes_from_settings(
    archive_root: Path, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TBD-37]: set it, and a claim 213 days out becomes urgent."""
    (claim_id,) = load(archive_root, "CLM-SET")
    set_filing_term(owner_engine, 365)
    item_id = denial_for(claim_id)

    monkeypatch.setenv("RCM_TIMELY_FILING_ESCALATION_DAYS", "300")
    get_settings.cache_clear()
    try:
        run(item_id, FAR)
    finally:
        monkeypatch.delenv("RCM_TIMELY_FILING_ESCALATION_DAYS")
        get_settings.cache_clear()

    assert get(item_id).deadline_pinned is True


# ---------------------------------------------------------------------------
# Unknown deadline: urgent, and the user is asked
# ---------------------------------------------------------------------------


def test_an_unknown_filing_term_pins_and_asks_once_per_payer(
    archive_root: Path, owner_engine: Engine
) -> None:
    """Owner decision (2026-11-01): ask for the deadline; until it is
    given, the claim is urgent. One question per payer, not per claim."""
    first, second = load(archive_root, "CLM-A", "CLM-B")
    set_filing_term(owner_engine, None)
    items = [denial_for(first), denial_for(second)]

    for item_id in items:
        run(item_id, FAR)

    assert all(get(i).deadline_pinned for i in items)
    with session_scope() as session:
        asks = session.scalars(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.MISSING_FILING_DEADLINE))
        ).all()
        assert len(asks) == 1
        assert "urgent" in asks[0].detail.lower()


# ---------------------------------------------------------------------------
# PD-1, PD-20
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_every_prioritisation_writes_a_decision_record(
    archive_root: Path, owner_engine: Engine
) -> None:
    (claim_id,) = load(archive_root, "CLM-PD1")
    set_filing_term(owner_engine, 365)
    item_id = denial_for(claim_id)

    run(item_id, FAR)

    with session_scope() as session:
        records = session.scalars(
            select(DecisionRecord).where(DecisionRecord.entity_id == item_id)
        ).all()
        assert len(records) == 1
        record = records[0]
        assert record.decision_type == "PRIORITISE"
        assert record.logic_version == LOGIC_VERSION
        assert record.input_snapshot["days_to_deadline"] == 213
        assert record.input_snapshot["balance"] == "500.00"
        assert record.output["score"] == get(item_id).priority_score


def test_another_clients_item_cannot_be_prioritised(
    archive_root: Path, owner_engine: Engine
) -> None:
    (claim_id,) = load(archive_root, "CLM-PD20")
    set_filing_term(owner_engine, 365)
    item_id = denial_for(claim_id)
    with pytest.raises(ResourceNotFoundError):
        run(item_id, FAR, client_id=OTHER_CLIENT)


# ---------------------------------------------------------------------------
# The queue read
# ---------------------------------------------------------------------------


def test_the_queue_reads_pinned_first_then_by_score(
    archive_root: Path, owner_engine: Engine
) -> None:
    """PD-15 at the moment an operator presses "next": the claim six days
    from its deadline comes first though the other is worth 100 times more."""
    big, small = load(archive_root, "CLM-BIG", "CLM-SMALL")
    set_filing_term(owner_engine, 365)
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE claim SET total_charge = 50000 WHERE submitter_claim_id = 'CLM-BIG'")
        )
        # 359 days before FAR: six days left on a 365-day term.
        conn.execute(
            text(
                "UPDATE claim SET date_of_service = '2023-06-08' "
                "WHERE submitter_claim_id = 'CLM-SMALL'"
            )
        )
    big_item, small_item = denial_for(big), denial_for(small)
    run(big_item, FAR)
    run(small_item, FAR)

    assert get(small_item).deadline_pinned is True
    assert get(big_item).deadline_pinned is False
    with session_scope() as session:
        order = [i.work_item_id for i in priority.next_items(session, client_id=CLIENT)]
    assert order == [small_item, big_item]


def test_bulk_work_is_kept_out_of_the_individual_queue(
    archive_root: Path, owner_engine: Engine
) -> None:
    """BR-M10-06: a $10 balance is bulk work, not a person's next item."""
    (claim_id,) = load(archive_root, "CLM-TINY", charge="10.00")
    set_filing_term(owner_engine, 365)
    item_id = denial_for(claim_id)
    run(item_id, FAR)

    assert get(item_id).bulk_handling is True
    with session_scope() as session:
        individual = [i.work_item_id for i in priority.next_items(session, client_id=CLIENT)]
        everything = [
            i.work_item_id
            for i in priority.next_items(session, client_id=CLIENT, include_bulk=True)
        ]
    assert item_id not in individual
    assert item_id in everything


def test_the_balance_is_what_is_still_owed(archive_root: Path) -> None:
    """With nothing paid, the balance is the full charge."""
    (claim_id,) = load(archive_root, "CLM-OWED")
    with session_scope() as session:
        balance = priority.outstanding_balance(session, claim_id=claim_id, client_id=CLIENT)
    assert balance == Decimal("500.00")
