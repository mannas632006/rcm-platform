"""Denials classified once, one category each, recorded (M4.8, ADR-0012).

BR-M11-01/02, BR-TAX-01: every denial classified by root cause, with one
category and one preventability determination.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from rcm.analytics.denials import denial_rate
from rcm.decision import denials
from rcm.decision.denial_models import DenialClassification
from rcm.domain.remittance import ClaimPayment
from rcm.platform.audit import DecisionRecord, DecisionType
from rcm.platform.db import reset_engines, session_scope
from rcm.work import denials as work_denials
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_acknowledgement_loading import (
    CHARGE,
    CLIENT_A,
    CLIENT_B,
    _remittance,
    denial_for,
    payment_for,
    remit,
    submit,
)
from tests.integration.test_claim_progression import TABLES

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE denial_classification, {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def one_line(claim_id: str, *cas: str, control: str) -> bytes:
    """An 835 paying nothing on one line, with the given CAS segments."""
    body = [
        f"CLP*{claim_id}*4*{CHARGE}*0.00**MB*PCN{claim_id}*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*0.00**1~",
        *cas,
    ]
    return _remittance(body, control=control, check=Decimal("0.00"))


def classify(client: uuid.UUID = CLIENT_A) -> denials.ClassificationReport:
    with session_scope() as session:
        return denials.classify_denials(session, client_id=client)


def rows() -> list[DenialClassification]:
    with session_scope() as session:
        return list(session.scalars(select(DenialClassification)).all())


def classified_claims() -> set[tuple[str, str, str]]:
    """(CLM01, category, preventability) per classified line."""
    with session_scope() as session:
        return {
            (claim_id, category, preventability)
            for claim_id, category, preventability in session.execute(
                select(
                    ClaimPayment.claim_id,
                    DenialClassification.category,
                    DenialClassification.preventability,
                ).join(
                    ClaimPayment,
                    ClaimPayment.claim_payment_id == DenialClassification.claim_payment_id,
                )
            )
        }


NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.mark.invariant
def test_every_denial_maps_to_one_category(root: Path) -> None:
    """M4.8 acceptance and BR-TAX-01."""
    submit("CLM-MN", root, control="0001")
    remit(denial_for("CLM-MN", control="0001"), root, name="mn.835")
    submit("CLM-AU", root, control="0002")
    remit(one_line("CLM-AU", f"CAS*CO*197*{CHARGE}~", control="0002"), root, name="au.835")
    report = classify()
    assert (report.classified, report.unmapped) == (2, [])
    assert classified_claims() == {
        ("CLM-MN", "MEDICAL_NECESSITY", "PARTIALLY_PREVENTABLE"),
        ("CLM-AU", "AUTHORISATION", "PREVENTABLE"),
    }


def test_a_paid_line_is_not_classified(root: Path) -> None:
    submit("CLM-PD", root, control="0001")
    remit(payment_for("CLM-PD", control="0001"), root, name="pd.835")
    assert classify().classified == 0
    assert rows() == []


def test_a_line_paid_nothing_for_cost_share_alone_is_not_a_denial(root: Path) -> None:
    submit("CLM-DED", root, control="0001")
    remit(one_line("CLM-DED", f"CAS*PR*1*{CHARGE}~", control="0001"), root, name="ded.835")
    assert classify().classified == 0


def test_pr_204_is_classified_as_a_denial(root: Path) -> None:
    """ADR-0012: the 135 claims progression held were these."""
    submit("CLM-204", root, control="0001")
    remit(one_line("CLM-204", f"CAS*PR*204*{CHARGE}~", control="0001"), root, name="204.835")
    assert classify().classified == 1
    (row,) = rows()
    assert (row.category, row.cas_group_code, row.carc_code) == (
        "CONTRACTUAL_NON_COVERED",
        "PR",
        "204",
    )


@pytest.mark.invariant
def test_an_unknown_refusal_is_reported_not_guessed(root: Path) -> None:
    submit("CLM-UNK", root, control="0001")
    remit(one_line("CLM-UNK", f"CAS*CO*A1*{CHARGE}~", control="0001"), root, name="unk.835")
    report = classify()
    assert report.classified == 0
    (line,) = report.unmapped
    assert line.carcs == ("A1",)
    assert rows() == []


@pytest.mark.invariant
def test_classifying_again_changes_nothing(root: Path) -> None:
    submit("CLM-MN", root, control="0001")
    remit(denial_for("CLM-MN", control="0001"), root, name="mn.835")
    classify()
    again = classify()
    # A classified line is not read again at all.
    assert (again.classified, again.already, again.unmapped) == (0, 0, [])
    (row,) = rows()
    # decision_record is append-only and outlives each test, so this test's
    # own claim payment picks out its records.
    with session_scope() as session:
        records = session.scalars(
            select(DecisionRecord).where(
                DecisionRecord.decision_type == str(DecisionType.CLASSIFY_DENIAL),
                DecisionRecord.entity_id == row.claim_payment_id,
            )
        ).all()
    assert len(records) == 1
    assert records[0].logic_version == denials.TAXONOMY_VERSION
    assert records[0].output["lines"][0]["category"] == "MEDICAL_NECESSITY"


def test_classification_reads_only_its_clients_denials(root: Path) -> None:
    """PD-20."""
    submit("CLM-A", root, control="0001")
    remit(denial_for("CLM-A", control="0001"), root, name="a.835")
    submit("CLM-B", root, client=CLIENT_B, control="0002")
    remit(denial_for("CLM-B", control="0002"), root, client=CLIENT_B, name="b.835")
    assert classify(CLIENT_A).classified == 1
    assert {r.client_id for r in rows()} == {CLIENT_A}


def test_a_classification_cannot_be_rewritten(root: Path, app_engine: Engine) -> None:
    submit("CLM-MN", root, control="0001")
    remit(denial_for("CLM-MN", control="0001"), root, name="mn.835")
    classify()
    for statement in (
        "UPDATE denial_classification SET category = 'PAYER_ERROR'",
        "DELETE FROM denial_classification",
    ):
        with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
            conn.execute(text(statement))
        assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)


@pytest.mark.invariant
def test_a_deductible_beside_co_45_is_not_a_denial(root: Path) -> None:
    """ADR-0012: allowed 120, all to the deductible, the rest cut by the fee
    schedule. Paid nothing, refused nothing."""
    submit("CLM-JAN", root, control="0001")
    remit(
        one_line("CLM-JAN", "CAS*CO*45*80.00~", "CAS*PR*1*120.00~", control="0001"),
        root,
        name="jan.835",
    )
    report = classify()
    assert (report.classified, report.not_refused, report.unmapped) == (0, 1, [])
    with session_scope() as session:
        assert denial_rate(session, client_id=CLIENT_A).denied_claims == 0


def test_a_reversed_denial_is_classified_and_counted_once(root: Path) -> None:
    """ADR-0012: the denial happened. The classifier and the denial rate both
    count it, once; the reversal posting, every sign flipped, is not a
    second refusal (BR-M9-04)."""
    submit("CLM-RV", root, control="0001")
    remit(denial_for("CLM-RV", control="0001"), root, name="rv.835")
    reversal = [
        f"CLP*CLM-RV*22*{-CHARGE}*0.00**MB*PCNCLM-RV*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{-CHARGE}*0.00**1~",
        f"CAS*CO*50*{-CHARGE}~",
    ]
    remit(_remittance(reversal, control="0002", check=Decimal("0.00")), root, name="rv2.835")
    assert classify().classified == 1
    assert classified_claims() == {("CLM-RV", "MEDICAL_NECESSITY", "PARTIALLY_PREVENTABLE")}
    with session_scope() as session:
        assert denial_rate(session, client_id=CLIENT_A).denied_claims == 1


@pytest.mark.invariant
def test_an_unmapped_denial_is_put_before_a_person(root: Path) -> None:
    """PD-18 and PD-1: never guessed, never only a count in a log line."""
    submit("CLM-UNK", root, control="0001")
    remit(one_line("CLM-UNK", f"CAS*CO*A1*{CHARGE}~", control="0001"), root, name="unk.835")
    for _ in range(2):
        with session_scope() as session:
            run = work_denials.classify_and_route(session, client_id=CLIENT_A, now=NOW)
        (line,) = run.classification.unmapped
    with session_scope() as session:
        items = session.scalars(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.DATA_QUALITY))
        ).all()
        records = session.scalars(
            select(DecisionRecord).where(
                DecisionRecord.decision_type == str(DecisionType.CLASSIFY_DENIAL),
                DecisionRecord.entity_id == line.service_line_id,
            )
        ).all()
    (item,) = items
    assert "A1" in item.title
    assert item.source_key is not None
    assert item.source_key.endswith(str(line.service_line_id))
    (record,) = records
    assert record.output == {"held": "unmapped-refusal", "category": None}
    assert record.input_snapshot == {"refusal_carcs": ["A1"]}


def test_a_concurrent_run_does_not_classify_twice(root: Path) -> None:
    """Two runs that both read the line unclassified: the second waits on the
    first's row, then skips it. One row, one decision record, no error."""
    submit("CLM-MN", root, control="0001")
    remit(denial_for("CLM-MN", control="0001"), root, name="mn.835")
    second: list[denials.ClassificationReport] = []
    with session_scope() as first:
        assert denials.classify_denials(first, client_id=CLIENT_A).classified == 1
        thread = threading.Thread(target=lambda: second.append(classify()))
        thread.start()
        # Let the second run read the line and block on the first's insert.
        time.sleep(1.0)
    thread.join(timeout=30)
    (report,) = second
    assert (report.classified, report.already) == (0, 1)
    (row,) = rows()
    with session_scope() as session:
        records = session.scalars(
            select(DecisionRecord).where(DecisionRecord.entity_id == row.claim_payment_id)
        ).all()
    assert len([r for r in records if r.decision_type == str(DecisionType.CLASSIFY_DENIAL)]) == 1


def test_a_classification_cannot_name_another_clients_line(root: Path) -> None:
    """PD-20: the composite foreign key ties the row to its line's client."""
    submit("CLM-MN", root, control="0001")
    remit(denial_for("CLM-MN", control="0001"), root, name="mn.835")
    with session_scope() as session:
        line_id, payment_id = session.execute(
            text("SELECT service_line_id, claim_payment_id FROM remittance_service_line")
        ).one()
    with pytest.raises(IntegrityError) as refused, session_scope() as session:
        session.add(
            DenialClassification(
                service_line_id=line_id,
                client_id=CLIENT_B,
                claim_payment_id=payment_id,
                taxonomy_version="taxonomy-v1",
                category="CODING",
                preventability="PREVENTABLE",
                cas_group_code="CO",
                carc_code="50",
            )
        )
        session.flush()
    assert isinstance(refused.value.orig, pg_errors.ForeignKeyViolation)
