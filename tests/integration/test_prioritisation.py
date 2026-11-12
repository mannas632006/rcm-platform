"""Prioritising real work items against real claims (M3.6).

PD-1: Every decision writes a decision_record with logic version,
inputs, output.
PD-15: Timely-filing deadline overrides all other prioritisation.
FR-WORK-06 (SHOULD): The queue exposes why an item ranks where it does.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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


def denial_for(claim_id: uuid.UUID, *, raised_at: datetime = FAR) -> uuid.UUID:
    with session_scope() as session:
        item = raise_exception(
            session,
            client_id=CLIENT,
            kind=WorkItemKind.DENIAL,
            problem="CO-50 medical necessity denial on line 1",
            record=RecordRef("claim", claim_id),
            source_key=f"denial:{claim_id}",
            now=raised_at,
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


def test_a_scrubber_block_near_the_deadline_is_pinned_first(
    archive_root: Path, owner_engine: Engine
) -> None:
    """EC-39 / BR-GLOBAL-04: a rule blocks a claim six days from its filing
    deadline. The deadline wins: the block is a person's next item, ahead of
    a denial worth 100 times more."""
    big, blocked = load(archive_root, "CLM-BIG", "CLM-BLOCKED")
    set_filing_term(owner_engine, 365)
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE claim SET total_charge = 50000 WHERE submitter_claim_id = 'CLM-BIG'")
        )
        # 359 days before FAR: six days left on a 365-day term.
        conn.execute(
            text(
                "UPDATE claim SET date_of_service = '2023-06-08' "
                "WHERE submitter_claim_id = 'CLM-BLOCKED'"
            )
        )
    big_item = denial_for(big)
    with session_scope() as session:
        block = raise_exception(
            session,
            client_id=CLIENT,
            kind=WorkItemKind.SCRUB_EXCEPTION,
            problem="Scrubber rule NO_MOD25_ON_EM v1 blocks line 1: review modifier 25",
            record=RecordRef("claim", blocked),
            source_key=f"scrub:NO_MOD25_ON_EM:1:{blocked}",
            now=FAR,
            claim_id=blocked,
        ).work_item_id
    run(big_item, FAR)
    run(block, FAR)

    pinned = get(block)
    assert (pinned.deadline_pinned, pinned.priority_score) == (True, 100)
    assert "6 days" in pinned.priority_reason
    with session_scope() as session:
        order = [i.work_item_id for i in priority.next_items(session, client_id=CLIENT)]
    assert order == [block, big_item]


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


@pytest.mark.invariant
def test_re_prioritising_the_missing_deadline_question_keeps_it_pinned(
    archive_root: Path, owner_engine: Engine
) -> None:
    """The question asking the user for a deadline is itself urgent work;
    prioritising it again must not drop it to the bottom."""
    (claim_id,) = load(archive_root, "CLM-ASK")
    set_filing_term(owner_engine, None)
    run(denial_for(claim_id), FAR)
    with session_scope() as session:
        ask = session.scalar(
            select(WorkItem.work_item_id).where(
                WorkItem.kind == str(WorkItemKind.MISSING_FILING_DEADLINE)
            )
        )
    assert ask is not None
    run(ask, FAR)
    assert get(ask).deadline_pinned is True


def test_an_unscored_item_is_not_treated_as_low_priority(
    archive_root: Path, owner_engine: Engine
) -> None:
    """PD-18: before it is prioritised an item's urgency is unknown, so it
    reads ahead of scored work, not behind it."""
    scored_claim, fresh_claim = load(archive_root, "CLM-S1", "CLM-S2")
    set_filing_term(owner_engine, 365)
    scored = denial_for(scored_claim)
    run(scored, FAR)
    fresh = denial_for(fresh_claim)

    with session_scope() as session:
        order = [i.work_item_id for i in priority.next_items(session, client_id=CLIENT)]
    assert order.index(fresh) < order.index(scored)


# ---------------------------------------------------------------------------
# Healthcare review findings
# ---------------------------------------------------------------------------


def test_an_accepted_claim_is_not_pinned_on_its_filing_deadline(
    archive_root: Path, owner_engine: Engine
) -> None:
    """Once the payer accepted the claim, timely filing is met for good."""
    (claim_id,) = load(archive_root, "CLM-ACC")
    set_filing_term(owner_engine, 365)
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE claim SET state = 'Denied', next_action_due = '2025-01-15'"))
    item_id = denial_for(claim_id)
    run(item_id, NEAR)
    assert get(item_id).deadline_pinned is False


def test_the_earliest_line_date_stands_in_for_a_missing_claim_date(
    archive_root: Path, owner_engine: Engine
) -> None:
    """837P dates the lines (DTP*472), not always the claim."""
    (claim_id,) = load(archive_root, "CLM-LINEDATE")
    set_filing_term(owner_engine, 365)
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE claim SET date_of_service = NULL"))
    item_id = denial_for(claim_id)
    run(item_id, FAR)
    assert get(item_id).days_to_deadline == 213
    with session_scope() as session:
        assert (
            session.scalar(select(WorkItem).where(WorkItem.kind == str(WorkItemKind.DATA_QUALITY)))
            is None
        )


def test_pinned_items_read_nearest_deadline_first(archive_root: Path, owner_engine: Engine) -> None:
    """Expiring tomorrow comes before expiring in ten days."""
    later, sooner = load(archive_root, "CLM-10D", "CLM-2D")
    set_filing_term(owner_engine, 365)
    with owner_engine.begin() as conn:
        # FAR is 2024-06-01: 355 days back leaves 10, 363 days back leaves 2.
        conn.execute(
            text(
                "UPDATE claim SET date_of_service = '2023-06-12' "
                "WHERE submitter_claim_id = 'CLM-10D'"
            )
        )
        conn.execute(
            text(
                "UPDATE claim SET date_of_service = '2023-06-04' "
                "WHERE submitter_claim_id = 'CLM-2D'"
            )
        )
    # The ten-day item is older, so every tie-breaker after the deadline key
    # (SLA, then age) would put it first: only the deadline key can pass.
    later_item = denial_for(later, raised_at=FAR - timedelta(days=1))
    sooner_item = denial_for(sooner)
    run(later_item, FAR)
    run(sooner_item, FAR)
    with session_scope() as session:
        order = [i.work_item_id for i in priority.next_items(session, client_id=CLIENT)]
    assert order.index(sooner_item) < order.index(later_item)


@pytest.mark.invariant
def test_reprioritising_daily_pins_a_claim_as_its_deadline_arrives(
    archive_root: Path, owner_engine: Engine
) -> None:
    """A pin set once and never refreshed lets a claim slip past the
    threshold. The daily re-score catches it."""
    (claim_id,) = load(archive_root, "CLM-DRIFT")
    set_filing_term(owner_engine, 365)
    item_id = denial_for(claim_id)
    run(item_id, FAR)
    assert get(item_id).deadline_pinned is False

    with session_scope() as session:
        assert priority.reprioritise_open(session, client_id=CLIENT, now=NEAR) >= 1
    assert get(item_id).deadline_pinned is True


def test_the_missing_deadline_question_names_the_payer(
    archive_root: Path, owner_engine: Engine
) -> None:
    (claim_id,) = load(archive_root, "CLM-NAME")
    set_filing_term(owner_engine, None)
    run(denial_for(claim_id), FAR)
    with session_scope() as session:
        ask = session.scalar(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.MISSING_FILING_DEADLINE))
        )
        assert ask is not None
        assert "00590" in ask.title
