"""277 status answers, loaded and put before a person (ADR-0033, EC-54, EC-56).

- A 277 answering a status enquiry (005010X212) is kept as an answer, never
  as a verdict or a denial (PD-4), and moves no claim.
- A claim the payer says it decided, with no 835 after the wait, is chased.
- A claim pended again and again without a reason goes to a person.
- One item per claim and cause while it is open; each raise is a decision.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import ProgrammingError

from rcm.domain.acknowledgement import ClaimAcknowledgement, ClaimStatusAnswer
from rcm.domain.claim import Claim
from rcm.domain.claim_state import ClaimState
from rcm.ingestion.acknowledgement_loader import load_acknowledgement_file
from rcm.platform.audit import DecisionRecord, DecisionType
from rcm.platform.config import get_settings
from rcm.platform.db import reset_engines, session_scope
from rcm.work import progression, status_follow_up
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_acknowledgement_loading import (
    CLIENT_A,
    CLIENT_B,
    _wrap,
    ack_277ca,
    acknowledge,
    payment_for,
    remit,
    submit,
)
from tests.integration.test_claim_progression import TABLES, state_of

pytestmark = pytest.mark.integration

RECEIVED = datetime(2024, 2, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE claim_status_answer, {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def status_277(*statuses: tuple[str, str], control: str = "0001", words: str = "") -> bytes:
    """A 277 answering a status enquiry for each (claim_id, "F1:65")."""
    body = [
        f"ST*277*{control}*005010X212~",
        f"BHT*0010*08*{control}*20240201*0000*DG~",
        "HL*1**20*1~",
        "NM1*PR*2*BCBS Texas*****PI*00590~",
        "HL*2*1*21*1~",
        "NM1*41*2*RCM SEED BILLING*****46*SUBMITTER01~",
        "HL*3*2*19*1~",
        "NM1*1P*2*Lakeview Clinic*****XX*4952654158~",
    ]
    level = 4
    for claim_id, composite in statuses:
        body.extend(
            [
                f"HL*{level}*3*22*0~",
                "NM1*IL*1*Castellanos*Andre****MI*VAZ957419823~",
                f"TRN*2*{claim_id}~",
                f"STC*{composite}*20240201**200.00********{words}~",
            ]
        )
        level += 1
    return _wrap(body, functional="HN", version="005010X212", control=control)


def advance(now: datetime, client: uuid.UUID = CLIENT_A) -> None:
    with session_scope() as session:
        progression.advance_all(session, client_id=client, now=now)


def accepted(claim_id: str, root: Path, *, client: uuid.UUID = CLIENT_A) -> None:
    submit(claim_id, root, client=client, control="0001")
    acknowledge(ack_277ca((claim_id, "A2:20")), root, client=client, name=f"{claim_id}.277ca")
    advance(RECEIVED, client)


def answer(
    claim_id: str,
    composite: str,
    root: Path,
    *,
    control: str = "0001",
    words: str = "",
    client: uuid.UUID = CLIENT_A,
) -> None:
    with session_scope() as session:
        result = load_acknowledgement_file(
            session,
            status_277((claim_id, composite), control=control, words=words),
            client_id=client,
            original_filename=f"{claim_id}-{control}.277",
            archive_root=root,
            received_at=RECEIVED,
        )
    assert not result.quarantined, result.quarantine_reason
    assert result.status_answers == 1


def follow_up(now: datetime, client: uuid.UUID = CLIENT_A) -> status_follow_up.StatusFollowUpReport:
    with session_scope() as session:
        return status_follow_up.follow_up_client(session, client_id=client, now=now)


def items() -> list[WorkItem]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(WorkItem).where(WorkItem.kind == str(WorkItemKind.CLAIM_STATUS_FOLLOW_UP))
            ).all()
        )


def after_grace() -> datetime:
    return RECEIVED + timedelta(days=get_settings().status_remittance_grace_days + 1)


# ---------------------------------------------------------------------------
# Loading: an answer, not a verdict
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_status_answer_is_not_a_verdict_and_moves_nothing(root: Path) -> None:
    """PD-4: "denied" on a 277 is not a denial. It lands in its own table,
    no acknowledgement is written, and the claim stays in adjudication."""
    accepted("CLM-SA", root)
    answer("CLM-SA", "F2:88", root)
    with session_scope() as session:
        (row,) = session.scalars(select(ClaimStatusAnswer)).all()
        assert (row.answer, row.category_code, row.status_code) == ("DENIED", "F2", "88")
        sources = session.scalars(select(ClaimAcknowledgement.source)).all()
        assert sources == ["277CA"]  # only the front-end acceptance
    advance(after_grace())
    assert state_of("CLM-SA") is ClaimState.IN_ADJUDICATION


def test_an_unknown_category_in_a_status_answer_quarantines_the_file(root: Path) -> None:
    result = acknowledge(status_277(("CLM-Q", "Q9:1")), root, name="q.277")
    assert result.quarantined
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(ClaimStatusAnswer)) == 0


# ---------------------------------------------------------------------------
# EC-54: the payer says decided; the 835 never came
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_claim_the_payer_says_it_paid_with_no_835_is_chased(root: Path) -> None:
    """EC-54. Not inside the wait; after it, one named item, and a recorded
    decision (PD-1)."""
    accepted("CLM-PD", root)
    answer("CLM-PD", "F1:65", root)
    assert follow_up(RECEIVED + timedelta(days=1)).raised == 0
    assert items() == []

    report = follow_up(after_grace())
    assert (report.raised, report.by_key) == (1, {"status-decided-no-835": 1})
    (item,) = items()
    assert "F1:65" in item.title
    assert "835" in item.title
    assert item.owner_role == "ar_specialist"
    with session_scope() as session:
        claim_id = session.scalar(
            select(Claim.claim_id).where(Claim.submitter_claim_id == "CLM-PD")
        )
        decision = session.scalars(
            select(DecisionRecord).where(
                DecisionRecord.entity_id == claim_id,
                DecisionRecord.decision_type == str(DecisionType.STATUS_FOLLOW_UP),
            )
        ).one()
        assert decision.output == {"follow_up": "status-decided-no-835"}
        assert decision.logic_version == status_follow_up.LOGIC_VERSION


def test_an_835_on_file_answers_the_claim_and_nothing_is_chased(root: Path) -> None:
    accepted("CLM-REM", root)
    answer("CLM-REM", "F1:65", root)
    remit(payment_for("CLM-REM", control="0001"), root, name="rem.835")
    assert follow_up(after_grace()).raised == 0
    assert items() == []


def test_following_up_twice_raises_once(root: Path) -> None:
    accepted("CLM-TW", root)
    answer("CLM-TW", "F1:65", root)
    assert follow_up(after_grace()).raised == 1
    assert follow_up(after_grace()).raised == 0
    assert len(items()) == 1


# ---------------------------------------------------------------------------
# EC-56: pended again and again, never saying why
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_repeated_pends_without_a_reason_go_to_a_person(root: Path) -> None:
    accepted("CLM-PN", root)
    limit = get_settings().status_pending_repeat_limit
    for n in range(1, limit):
        answer("CLM-PN", "P1:20", root, control=f"000{n}")
    assert follow_up(RECEIVED).raised == 0
    answer("CLM-PN", "P1:20", root, control=f"000{limit}")
    report = follow_up(RECEIVED)
    assert report.by_key == {"status-pending-no-reason": 1}
    (item,) = items()
    assert f"{limit} times" in item.title


def test_pends_that_state_a_reason_are_waited_out(root: Path) -> None:
    accepted("CLM-PR", root)
    for n in range(1, get_settings().status_pending_repeat_limit + 1):
        answer("CLM-PR", "P1:20", root, control=f"000{n}", words="Awaiting medical review")
    assert follow_up(RECEIVED).raised == 0
    assert items() == []


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_a_claim_under_appeal_is_not_worked_here(root: Path, owner_engine: Engine) -> None:
    """EC-57: an appealing claim is the appeal's."""
    accepted("CLM-AP", root)
    answer("CLM-AP", "R4:15", root)
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE claim SET state = 'Appealing' WHERE submitter_claim_id = 'CLM-AP'")
        )
    assert follow_up(RECEIVED).claims_read == 0
    assert items() == []


def test_another_clients_answers_are_never_read(root: Path) -> None:
    """PD-20: the answer and the claim are client B's."""
    accepted("CLM-B", root, client=CLIENT_B)
    answer("CLM-B", "R4:15", root, client=CLIENT_B)
    assert follow_up(RECEIVED, client=CLIENT_A).claims_read == 0
    assert items() == []
    assert follow_up(RECEIVED, client=CLIENT_B).raised == 1


def test_status_answers_are_insert_only(app_engine: Engine, root: Path) -> None:
    """What a payer said is evidence, and is not rewritten."""
    accepted("CLM-IO", root)
    answer("CLM-IO", "P1:20", root)
    for statement in (
        "UPDATE claim_status_answer SET answer = 'PAID'",
        "DELETE FROM claim_status_answer",
    ):
        with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
            conn.execute(text(statement))
        assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)
