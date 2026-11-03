"""NCCI releases: loaded whole, write-once, pinned by the ruleset (M4.3, ADR-0008)."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import ProgrammingError

from rcm.decision import ncci_store
from rcm.decision.ncci_models import NcciPtpEdit, NcciRelease
from rcm.decision.rules import scrub, store
from rcm.decision.rules.evaluate import Outcome, Verdict
from rcm.domain.claim import Claim
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.x12 import ClaimServiceLine
from rcm.platform.audit import DecisionRecord
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ValidationFailedError
from tests.integration.test_claim_loading import claim, file_for
from tests.integration.test_rules_engine import CLIENT, TABLES
from tests.unit.test_ncci import MUE_CSV, PTP_CSV

pytestmark = pytest.mark.integration

ALL = f"ncci_mue, ncci_ptp_edit, ncci_release, {TABLES}"


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


def load(kind: str, payload: bytes, label: str = "2026Q1") -> ncci_store.LoadedRelease:
    with session_scope() as session:
        return ncci_store.load_release(
            session, kind=kind, label=label, payload=payload, loaded_by="coding_lead"
        )


def publish() -> store.PublishedRuleset:
    with session_scope() as session:
        return store.publish_ruleset(session, published_by="ar_lead")


def edits_loaded() -> int:
    with session_scope() as session:
        return session.scalar(select(func.count()).select_from(NcciPtpEdit)) or 0


def test_a_release_loads_every_row() -> None:
    loaded = load("PTP", PTP_CSV)
    assert loaded.release_id == "PTP:2026Q1"
    assert (loaded.row_count, loaded.created) == (3, True)
    assert edits_loaded() == 3


@pytest.mark.invariant
def test_loading_the_same_file_again_changes_nothing() -> None:
    load("PTP", PTP_CSV)
    again = load("PTP", PTP_CSV)
    assert again.created is False
    assert edits_loaded() == 3


def test_a_different_file_under_a_loaded_label_is_refused() -> None:
    load("PTP", PTP_CSV)
    changed = PTP_CSV.replace(b"45385,45380,19960101,*,1", b"45385,45380,19960101,*,0")
    with pytest.raises(ValidationFailedError):
        load("PTP", changed)
    with session_scope() as session:
        row = session.get(NcciRelease, "PTP:2026Q1")
        assert row is not None
        assert row.sha256 == ncci_store.sha256(PTP_CSV)


@pytest.mark.invariant
def test_a_malformed_file_loads_nothing() -> None:
    with pytest.raises(ValidationFailedError):
        load("PTP", PTP_CSV + b"45385,45380,19960101,*,7,bad\n")
    assert edits_loaded() == 0
    with session_scope() as session:
        assert session.scalar(select(NcciRelease)) is None


def test_a_bad_label_or_kind_is_refused() -> None:
    with pytest.raises(ValidationFailedError):
        load("PTP", PTP_CSV, label="2026 Q1")
    with pytest.raises(ValidationFailedError):
        load("LCD", PTP_CSV)


def test_ncci_rows_cannot_be_rewritten(app_engine: Engine) -> None:
    load("PTP", PTP_CSV)
    for statement in (
        "UPDATE ncci_ptp_edit SET modifier_indicator = '1'",
        "DELETE FROM ncci_ptp_edit",
        "UPDATE ncci_release SET sha256 = 'x'",
    ):
        with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
            conn.execute(text(statement))
        assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)


# ---------------------------------------------------------------------------
# Pinned by the ruleset, used by the scrub
# ---------------------------------------------------------------------------


def test_a_ruleset_pins_the_latest_releases() -> None:
    load("PTP", PTP_CSV)
    load("MUE", MUE_CSV)
    published = publish()
    with session_scope() as session:
        loaded = store.load_ruleset(session)
    assert (loaded.ncci_ptp_release, loaded.ncci_mue_release) == ("PTP:2026Q1", "MUE:2026Q1")
    assert published.digest == loaded.digest


def test_a_new_release_changes_the_next_rulesets_digest() -> None:
    load("PTP", PTP_CSV)
    first = publish()
    load("PTP", PTP_CSV.replace(b",1,Standards", b",0,Standards"), label="2026Q2")
    second = publish()
    with session_scope() as session:
        assert store.load_ruleset(session).ncci_ptp_release == "PTP:2026Q2"
    assert first.digest != second.digest


def test_a_ruleset_with_no_ncci_says_so() -> None:
    publish()
    with session_scope() as session:
        loaded = store.load_ruleset(session)
    assert (loaded.ncci_ptp_release, loaded.ncci_mue_release) == (None, None)


@pytest.mark.invariant
def test_a_scrub_applies_and_records_the_pinned_ncci_release(root: Path) -> None:
    """FR-RULES-01: the ruleset version still names everything that
    evaluated the claim, NCCI included."""
    load("PTP", PTP_CSV)
    publish()
    lines = tuple(
        ClaimServiceLine(
            line_number=n,
            procedure_code=code,
            charge_amount=Decimal("100.00"),
            diagnosis_pointers=(1,),
            service_date=claim("X").service_date,
        )
        for n, code in ((1, "45385"), (2, "45380"))
    )
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim("CLM-N1", charge="200.00", lines=lines)),
            client_id=CLIENT,
            original_filename="n1.edi",
            archive_root=root,
        )
    with session_scope() as session:
        claim_id = session.scalar(
            select(Claim.claim_id).where(Claim.submitter_claim_id == "CLM-N1")
        )
        assert claim_id is not None
        result = scrub.scrub_claim(session, claim_id=claim_id, client_id=CLIENT)
    assert result.evaluation.verdict is Verdict.BLOCK
    assert [(f.rule_id, f.line_number, f.outcome) for f in result.evaluation.findings] == [
        ("NCCI_PTP", 2, Outcome.FLAGGED)
    ]

    with session_scope() as session:
        record = session.scalar(select(DecisionRecord).where(DecisionRecord.entity_id == claim_id))
        assert record is not None
    assert record.input_snapshot["ncci_ptp_release"] == "PTP:2026Q1"
    assert record.output["findings"][0]["reference"] == "ncci-ptp PTP:2026Q1 45385/45380 mi=1"
