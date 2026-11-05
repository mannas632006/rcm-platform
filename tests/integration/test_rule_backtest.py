"""Backtest a rule against claims whose outcome is known (M4.4, ADR-0009).

BR-M7-04: "Every rule MUST be testable against historical claims before
deployment, with its false-positive rate measured."

Five claims, one of each kind: the rule flags charges of 400 or more.

    CLM-TP  500  denied   flagged   true positive
    CLM-FP  500  paid     flagged   false positive
    CLM-FN  100  denied   passed    missed denial
    CLM-TN  100  paid     passed
    CLM-NO  500  no 835   --        no outcome yet
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from rcm.decision.rules import backtest, store
from rcm.decision.rules.models import RuleBacktest
from rcm.ingestion.claim_loader import load_claim_file
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ResourceNotFoundError
from tests.integration.test_acknowledgement_loading import (
    CLIENT_A,
    CLIENT_B,
    denial_for,
    payment_for,
    remit,
)
from tests.integration.test_claim_loading import claim, file_for
from tests.integration.test_rules_engine import TABLES

pytestmark = pytest.mark.integration

ALL = (
    "rule_backtest, remittance_adjustment, remittance_service_line, claim_payment, "
    f"provider_adjustment, remittance, {TABLES}"
)


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {ALL} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


BIG_CHARGES: dict[str, Any] = {
    "rule_id": "BIG_CHARGE",
    "layer": "PAYER_SPECIFIC",
    "severity": "BLOCKING",
    "condition": {"field": "claim.total_charge", "op": "gte", "value": "400.00"},
    "action": {"type": "FLAG", "message": "Large charge: review", "route_to": "SCRUB_EXCEPTION"},
}


def load(
    claim_id: str, charge: str, root: Path, *, control: str, client: uuid.UUID = CLIENT_A
) -> None:
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim(claim_id, charge=charge), control=control),
            client_id=client,
            original_filename=f"{claim_id}.edi",
            archive_root=root,
        )


def corpus(root: Path) -> None:
    for index, (claim_id, charge, outcome) in enumerate(
        [
            ("CLM-TP", "500.00", "denied"),
            ("CLM-FP", "500.00", "paid"),
            ("CLM-FN", "100.00", "denied"),
            ("CLM-TN", "100.00", "paid"),
            ("CLM-NO", "500.00", None),
        ],
        start=1,
    ):
        load(claim_id, charge, root, control=f"{index:04d}")
        if outcome is not None:
            build = denial_for if outcome == "denied" else payment_for
            remit(build(claim_id, control=f"{index:04d}"), root, name=f"{claim_id}.835")


def draft(document: dict[str, Any] = BIG_CHARGES) -> int:
    with session_scope() as session:
        return store.create_rule_version(session, document=document, created_by="ar_lead")


def run(version: int, client: uuid.UUID = CLIENT_A) -> backtest.BacktestResult:
    with session_scope() as session:
        return backtest.run_backtest(
            session, rule_id="BIG_CHARGE", version=version, client_id=client, ran_by="ar_lead"
        )


@pytest.mark.invariant
def test_a_backtest_reports_what_the_rule_would_have_done(root: Path) -> None:
    corpus(root)
    result = run(draft())
    t = result.tally
    assert (t.evaluated, t.flagged, t.true_positives, t.false_positives) == (4, 2, 1, 1)
    assert (t.missed_denials, t.indeterminate, t.without_outcome) == (1, 0, 1)
    assert t.fp_rate == Decimal("0.5000")


def test_a_draft_can_be_backtested_without_going_live(root: Path) -> None:
    """The point of a backtest is to run before deployment."""
    from rcm.decision.rules.definition import RuleStatus
    from rcm.decision.rules.models import RuleVersion

    corpus(root)
    version = draft()
    run(version)
    with session_scope() as session:
        row = session.get(RuleVersion, ("BIG_CHARGE", version))
        assert row is not None
        assert row.status == str(RuleStatus.DRAFT)


@pytest.mark.invariant
def test_every_backtest_is_kept_with_its_rule_version(root: Path) -> None:
    """M4.5 refuses promotion without one, so it must be on record."""
    corpus(root)
    version = draft()
    result = run(version)
    with session_scope() as session:
        row = session.get(RuleBacktest, result.backtest_id)
        assert row is not None
        assert (row.rule_id, row.version, row.client_id) == ("BIG_CHARGE", version, CLIENT_A)
        assert (row.claims_evaluated, row.flagged, row.false_positives) == (4, 2, 1)
        assert row.fp_rate == Decimal("0.5000")


def test_a_backtest_reads_only_its_clients_claims(root: Path) -> None:
    """PD-20: another client's history is not this client's evidence."""
    corpus(root)
    load("CLM-B1", "900.00", root, control="0099", client=CLIENT_B)
    remit(denial_for("CLM-B1", control="0099"), root, client=CLIENT_B, name="b1.835")
    # Every count, not just "evaluated": a leaked claim with no outcome on
    # this client's side would only show up as `without_outcome`.
    ours = run(draft()).tally
    assert (ours.evaluated, ours.without_outcome, ours.flagged) == (4, 1, 2)
    theirs = run(1, CLIENT_B).tally
    assert (theirs.evaluated, theirs.without_outcome, theirs.flagged) == (1, 0, 1)


def test_an_unknown_rule_cannot_be_backtested() -> None:
    with pytest.raises(ResourceNotFoundError):
        run(7)


def test_a_backtest_record_cannot_be_rewritten(root: Path, app_engine: Engine) -> None:
    corpus(root)
    run(draft())
    for statement in ("UPDATE rule_backtest SET fp_rate = 0", "DELETE FROM rule_backtest"):
        with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
            conn.execute(text(statement))
        assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)
