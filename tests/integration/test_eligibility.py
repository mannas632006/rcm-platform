"""Stored 271s, and the scrub's question: covered on the date of service?
(DOC-02 M3; ADR-0034; EC-13, EC-15, EC-61)

The claim fixture is CPT 99203 for member VAZ957419823 of payer 00590, date
of service 2024-01-01, sent 2024-01-01, with a published ruleset holding no
rules: whatever the scrub finds is the eligibility check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ProgrammingError

from rcm.decision import eligibility, feedback
from rcm.decision.feedback_models import DenialFeedback
from rcm.decision.rules.evaluate import Mode, Verdict
from rcm.decision.rules.scrub import scrub_claim
from rcm.decision.rules.store import publish_ruleset
from rcm.domain.claim import Claim
from rcm.domain.eligibility import EligibilityResponseRecord
from rcm.ingestion.acknowledgement_loader import load_acknowledgement_file
from rcm.platform.audit import DecisionRecord, DecisionType
from rcm.platform.db import reset_engines, session_scope
from rcm.work import denials as work_denials
from tests.integration.test_acknowledgement_loading import (
    CHARGE,
    CLIENT_A,
    CLIENT_B,
    _wrap,
    remit,
    submit,
)
from tests.integration.test_denial_classification import one_line
from tests.integration.test_rule_backtest import ALL

pytestmark = pytest.mark.integration

MEMBER = "VAZ957419823"
DOS = date(2024, 1, 1)
SCRUBBED = date(2024, 1, 10)
OWNER = "test:eligibility"


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE eligibility_response, denial_feedback, denial_classification, "
                    f"rule_status_change, {ALL} RESTART IDENTITY CASCADE"
                )
            )

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


@pytest.fixture
def claim(root: Path) -> uuid.UUID:
    submit("CLM-EL", root, control="0001")
    with session_scope() as session:
        publish_ruleset(session, published_by=OWNER)
        found = session.scalar(select(Claim.claim_id).where(Claim.submitter_claim_id == "CLM-EL"))
    assert found is not None
    return found


def response_271(
    *segments: str,
    member: str = MEMBER,
    payer: str = "00590",
    control: str = "0001",
) -> bytes:
    return _wrap(
        [
            f"ST*271*{control}*005010X279A1~",
            f"BHT*0022*11*REQ{control}*20240102*0000~",
            "HL*1**20*1~",
            f"NM1*PR*2*BCBS Texas*****PI*{payer}~",
            "HL*2*1*21*1~",
            "NM1*1P*2*Lakeview Clinic*****XX*4952654158~",
            "HL*3*2*22*0~",
            f"NM1*IL*1*Castellanos*Andre****MI*{member}~",
            "DMG*D8*19640111*F~",
            *segments,
        ],
        functional="HB",
        version="005010X279A1",
        control=control,
    )


def load_271(
    root: Path,
    payload: bytes,
    *,
    received: date = date(2024, 1, 2),
    client: uuid.UUID = CLIENT_A,
    name: str = "271.edi",
) -> None:
    with session_scope() as session:
        result = load_acknowledgement_file(
            session,
            payload,
            client_id=client,
            original_filename=name,
            archive_root=root,
            received_at=datetime(received.year, received.month, received.day, 9, tzinfo=UTC),
        )
    assert result.eligibility_responses == 1


ACTIVE_ALL_YEAR = ("EB*1*IND*30**Gold PPO~", "DTP*346*RD8*20240101-20241231~")
TERMINATED_BEFORE_DOS = ("EB*6*IND*30**Gold PPO~", "DTP*346*RD8*20231201-20231231~")


def scrub(claim_id: uuid.UUID, today: date = SCRUBBED):  # type: ignore[no-untyped-def]
    with session_scope() as session:
        result = scrub_claim(session, claim_id=claim_id, client_id=CLIENT_A, today=today)
        record = session.scalars(
            select(DecisionRecord)
            .where(
                DecisionRecord.entity_id == claim_id,
                DecisionRecord.decision_type == str(DecisionType.SCRUB),
            )
            .order_by(DecisionRecord.decided_at.desc())
        ).first()
        assert record is not None
        snapshot = record.input_snapshot["eligibility"]
        findings = [
            (f.rule_id, f.mode, f.outcome)
            for f in result.evaluation.findings
            if f.rule_id == eligibility.ELIGIBILITY_CHECK
        ]
        # The scrub corrects claims it passes; these tests keep none of it.
        session.rollback()
    return result.evaluation.verdict, findings, snapshot


# ---------------------------------------------------------------------------
# Storing the 271
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_271_is_stored_without_the_subscribers_name_or_birth_date(root: Path) -> None:
    """ADR-0034, PD-13: the payer, the member and the benefits are kept; the
    name and date of birth stay in the archived file."""
    load_271(root, response_271(*ACTIVE_ALL_YEAR))
    with session_scope() as session:
        (row,) = session.scalars(select(EligibilityResponseRecord)).all()
        assert (row.payer_id, row.member_id, row.answered) == ("00590", MEMBER, True)
        (benefit,) = row.benefits
        assert benefit["code"] == "1"
        assert benefit["periods"] == [
            {"qualifier": "346", "start": "2024-01-01", "end": "2024-12-31"}
        ]
        stored = str(
            {c.name: getattr(row, c.name) for c in EligibilityResponseRecord.__table__.columns}
        )
    assert "Castellanos" not in stored
    assert "Andre" not in stored
    assert "1964" not in stored


def test_eligibility_responses_are_insert_only(app_engine: Engine, root: Path) -> None:
    load_271(root, response_271(*ACTIVE_ALL_YEAR))
    for statement in (
        "UPDATE eligibility_response SET answered = false",
        "DELETE FROM eligibility_response",
    ):
        with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
            conn.execute(text(statement))
        assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)


# ---------------------------------------------------------------------------
# The scrub: covered on the date of service?
# ---------------------------------------------------------------------------


def test_no_271_on_file_is_no_finding(claim: uuid.UUID) -> None:
    verdict, findings, snapshot = scrub(claim)
    assert (verdict, findings, snapshot) == (Verdict.PASS, [], None)


def test_coverage_on_the_date_of_service_passes(claim: uuid.UUID, root: Path) -> None:
    load_271(root, response_271(*ACTIVE_ALL_YEAR))
    verdict, findings, snapshot = scrub(claim)
    assert (verdict, findings) == (Verdict.PASS, [])
    assert snapshot["answered"] is True
    assert snapshot["active_on_date_of_service"] is True


@pytest.mark.invariant
def test_no_coverage_on_the_date_of_service_blocks(claim: uuid.UUID, root: Path) -> None:
    """EC-13: the payer's 271 shows coverage ended before the date of service."""
    load_271(root, response_271(*TERMINATED_BEFORE_DOS))
    verdict, findings, snapshot = scrub(claim)
    assert verdict is Verdict.BLOCK
    assert findings == [(eligibility.ELIGIBILITY_CHECK, "BLOCKING", "FLAGGED")]
    assert snapshot["active_on_date_of_service"] is False


def test_a_retroactive_termination_is_what_the_next_scrub_reads(
    claim: uuid.UUID, root: Path
) -> None:
    """EC-13: active when first checked; a later 271 says coverage ended
    before the date of service. The latest answer decides, and each scrub
    names the response it read."""
    load_271(root, response_271(*ACTIVE_ALL_YEAR), received=date(2024, 1, 2))
    first, _, before = scrub(claim, today=date(2024, 1, 5))
    load_271(
        root,
        response_271("EB*6*IND*30**Gold PPO~", "DTP*346*RD8*20231201-20231231~", control="0002"),
        received=date(2024, 1, 20),
        name="271-retro.edi",
    )
    # A scrub dated before the second answer arrived still reads the first.
    assert scrub(claim, today=date(2024, 1, 5))[0] is Verdict.PASS
    after_verdict, _, after = scrub(claim, today=date(2024, 1, 25))
    assert (first, after_verdict) == (Verdict.PASS, Verdict.BLOCK)
    assert before["response_id"] != after["response_id"]


def test_a_payer_that_could_not_look_is_advisory_not_a_denial(claim: uuid.UUID, root: Path) -> None:
    """PD-18: an AAA is "we could not look", not "not covered"."""
    load_271(root, response_271("AAA*Y**72*C~"))
    verdict, findings, snapshot = scrub(claim)
    assert verdict is Verdict.ADVISE
    assert findings == [(eligibility.ELIGIBILITY_CHECK, Mode.ADVISORY, "FLAGGED")]
    assert snapshot["answered"] is False


def test_coverage_confirmed_only_generically_is_advisory(claim: uuid.UUID, root: Path) -> None:
    """EC-15: "active", with no period and no detail about any service."""
    load_271(root, response_271("EB*1*IND*30~"))
    verdict, findings, _ = scrub(claim)
    assert verdict is Verdict.ADVISE
    assert findings == [(eligibility.ELIGIBILITY_CHECK, Mode.ADVISORY, "FLAGGED")]


def test_another_members_or_clients_271_is_never_read(claim: uuid.UUID, root: Path) -> None:
    """PD-20, and the member is the member: neither a 271 for someone else
    nor another client's 271 for the same member decides this claim."""
    load_271(root, response_271(*TERMINATED_BEFORE_DOS, member="XYZ000000001"), name="a.edi")
    load_271(root, response_271(*TERMINATED_BEFORE_DOS), client=CLIENT_B, name="b.edi")
    verdict, findings, snapshot = scrub(claim)
    assert (verdict, findings, snapshot) == (Verdict.PASS, [], None)


# ---------------------------------------------------------------------------
# EC-61: a denial the payer's own 271 contradicts
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_coverage_denial_the_payers_own_271_contradicts_drafts_no_rule(root: Path) -> None:
    """EC-61: the payer said covered before the claim was sent, then denied
    for coverage ending before the date of service. No rule can catch the
    payer contradicting itself: the reason is recorded, and it is appealed."""
    load_271(root, response_271(*ACTIVE_ALL_YEAR), received=date(2023, 12, 31))
    submit("CLM-61", root, control="0001")
    remit(one_line("CLM-61", f"CAS*CO*27*{CHARGE}~", control="0001"), root, name="61.835")
    with session_scope() as session:
        work_denials.classify_and_route(session, client_id=CLIENT_A, now=datetime.now(UTC))
    with session_scope() as session:
        (answer,) = session.scalars(select(DenialFeedback)).all()
        assert answer.outcome == str(feedback.FeedbackOutcome.NO_RULE)
        assert "own eligibility response" in answer.reason


def test_a_coverage_denial_with_no_contrary_271_drafts_its_candidate(root: Path) -> None:
    """Without that 271 the loop drafts as before: the contradiction is the
    whole reason for the exception."""
    submit("CLM-27", root, control="0001")
    remit(one_line("CLM-27", f"CAS*CO*27*{CHARGE}~", control="0001"), root, name="27.835")
    with session_scope() as session:
        work_denials.classify_and_route(session, client_id=CLIENT_A, now=datetime.now(UTC))
    with session_scope() as session:
        (answer,) = session.scalars(select(DenialFeedback)).all()
        assert answer.outcome == str(feedback.FeedbackOutcome.CANDIDATE_DRAFTED)
