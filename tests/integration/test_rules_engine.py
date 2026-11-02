"""Rules stored as versioned data, published as rulesets, applied to claims
(M4.1, M4.2, ADR-0007).

FR-RULES-01 / BR-M7-02: "Rules are versioned; every evaluated claim records
the ruleset version applied."

Rule status is set directly in `live()` below. That is test setup: the
promotion path that guards status changes (backtest first) is M4.5's.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError

from rcm.decision.rules import scrub, store
from rcm.decision.rules.definition import RuleStatus
from rcm.decision.rules.evaluate import Outcome, Verdict
from rcm.decision.rules.models import RuleVersion
from rcm.domain.claim import Claim
from rcm.ingestion.claim_loader import load_claim_file
from rcm.platform.audit import DecisionRecord, DecisionType
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ResourceNotFoundError, ValidationFailedError
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.integration

CLIENT = uuid.UUID("4e4e4e4e-0000-4000-8000-00000000004e")
OTHER = uuid.UUID("4f4f4f4f-0000-4000-8000-00000000004f")
TABLES = (
    "ruleset_member, ruleset, rule_version, submission_outbox, automation_suspension, "
    "work_item, claim_state_transition, claim_acknowledgement, claim_line, claim, raw_file, "
    "payer_alias, payer, payer_match_candidate"
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


def rule_doc(rule_id: str = "NO_MOD25_ON_EM", **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "rule_id": rule_id,
        "layer": "PAYER_SPECIFIC",
        "severity": "BLOCKING",
        "scope": {"cpt": ["99203"]},
        "condition": {"field": "line.modifiers", "op": "not_contains", "value": "25"},
        "action": {
            "type": "FLAG",
            "message": "This payer needs modifier 25 reviewed on this E/M line",
            "route_to": "SCRUB_EXCEPTION",
        },
    }
    values.update(overrides)
    return values


def create(document: dict[str, Any]) -> int:
    with session_scope() as session:
        return store.create_rule_version(session, document=document, created_by="ar_lead")


def live(rule_id: str, version: int, status: RuleStatus = RuleStatus.BLOCKING) -> None:
    """Test setup only; see the module docstring."""
    with session_scope() as session:
        session.execute(
            update(RuleVersion)
            .where(RuleVersion.rule_id == rule_id, RuleVersion.version == version)
            .values(status=str(status))
        )


def publish() -> store.PublishedRuleset:
    with session_scope() as session:
        return store.publish_ruleset(session, published_by="ar_lead")


def load_claim(root: Path, claim_id: str = "CLM-S1", client: uuid.UUID = CLIENT) -> uuid.UUID:
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim(claim_id)),
            client_id=client,
            original_filename=f"{claim_id}.edi",
            archive_root=root,
        )
        found = session.scalar(
            select(Claim.claim_id).where(
                Claim.submitter_claim_id == claim_id, Claim.client_id == client
            )
        )
        assert found is not None
        return found


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


# ---------------------------------------------------------------------------
# M4.1: versioned, never edited
# ---------------------------------------------------------------------------


def test_versions_count_up_from_one() -> None:
    assert create(rule_doc()) == 1
    assert create(rule_doc(severity="ADVISORY")) == 2
    assert create(rule_doc("ANOTHER_RULE")) == 1


def test_a_new_version_starts_as_a_draft() -> None:
    create(rule_doc())
    with session_scope() as session:
        row = session.get(RuleVersion, ("NO_MOD25_ON_EM", 1))
        assert row is not None
        assert row.status == str(RuleStatus.DRAFT)


def test_an_invalid_rule_is_never_stored() -> None:
    with pytest.raises(ValidationFailedError):
        create(rule_doc(condition={"field": "claim.member_id", "op": "eq", "value": "x"}))
    with session_scope() as session:
        assert session.scalar(select(RuleVersion)) is None


@pytest.mark.invariant
def test_a_stored_rule_cannot_be_rewritten(app_engine: Engine) -> None:
    """ADR-0007 Decision 1: the content of a rule version is fixed, so
    "which rule flagged this claim" has one answer. Only its lifecycle
    status may change."""
    create(rule_doc())
    with app_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text("UPDATE rule_version SET status = 'SHADOW' WHERE rule_id = 'NO_MOD25_ON_EM'")
            )
            with pytest.raises(ProgrammingError) as refused:
                conn.execute(
                    text(
                        "UPDATE rule_version SET document = '{}'::jsonb "
                        "WHERE rule_id = 'NO_MOD25_ON_EM'"
                    )
                )
            assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)
        finally:
            transaction.rollback()
    with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
        conn.execute(text("DELETE FROM rule_version"))
    assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)


def test_only_one_version_of_a_rule_is_live_at_a_time() -> None:
    create(rule_doc())
    create(rule_doc(severity="ADVISORY"))
    live("NO_MOD25_ON_EM", 1)
    with pytest.raises(IntegrityError):
        live("NO_MOD25_ON_EM", 2, RuleStatus.ADVISORY)


# ---------------------------------------------------------------------------
# Rulesets
# ---------------------------------------------------------------------------


def test_a_ruleset_holds_exactly_the_live_rules() -> None:
    create(rule_doc())
    create(rule_doc("STILL_A_DRAFT"))
    create(rule_doc("IN_SHADOW"))
    live("NO_MOD25_ON_EM", 1)
    live("IN_SHADOW", 1, RuleStatus.SHADOW)
    published = publish()
    assert published.version == 1
    with session_scope() as session:
        loaded = store.load_ruleset(session)
    assert loaded.version == 1
    assert sorted((r.rule.rule_id, r.status) for r in loaded.rules) == [
        ("IN_SHADOW", RuleStatus.SHADOW),
        ("NO_MOD25_ON_EM", RuleStatus.BLOCKING),
    ]


def test_a_status_change_waits_for_the_next_publish() -> None:
    create(rule_doc())
    live("NO_MOD25_ON_EM", 1)
    first = publish()
    live("NO_MOD25_ON_EM", 1, RuleStatus.ADVISORY)
    with session_scope() as session:
        assert store.load_ruleset(session).rules[0].status is RuleStatus.BLOCKING
    second = publish()
    assert second.version == first.version + 1
    assert second.digest != first.digest


def test_the_same_rules_give_the_same_digest() -> None:
    create(rule_doc())
    live("NO_MOD25_ON_EM", 1)
    assert publish().digest == publish().digest


def test_a_ruleset_cannot_be_rewritten(app_engine: Engine) -> None:
    create(rule_doc())
    live("NO_MOD25_ON_EM", 1)
    publish()
    for statement in ("UPDATE ruleset SET digest = 'x'", "DELETE FROM ruleset_member"):
        with app_engine.connect() as conn, pytest.raises(ProgrammingError) as refused:
            conn.execute(text(statement))
        assert isinstance(refused.value.orig, pg_errors.InsufficientPrivilege)


# ---------------------------------------------------------------------------
# M4.2: scrubbing a claim
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_scrubbing_with_no_ruleset_published_fails_closed(root: Path) -> None:
    """PD-18: no ruleset is not "no problems found"."""
    claim_id = load_claim(root)
    with session_scope() as session, pytest.raises(ResourceNotFoundError):
        scrub.scrub_claim(session, claim_id=claim_id, client_id=CLIENT)


@pytest.mark.invariant
def test_every_evaluated_claim_records_its_ruleset_version(root: Path) -> None:
    """FR-RULES-01, BR-M7-02, PD-1."""
    create(rule_doc())
    live("NO_MOD25_ON_EM", 1)
    publish()
    claim_id = load_claim(root)
    with session_scope() as session:
        result = scrub.scrub_claim(session, claim_id=claim_id, client_id=CLIENT)
    assert result.ruleset_version == 1
    assert result.evaluation.verdict is Verdict.BLOCK
    assert [f.outcome for f in result.evaluation.findings] == [Outcome.FLAGGED]

    with session_scope() as session:
        (record,) = session.scalars(
            select(DecisionRecord).where(DecisionRecord.entity_id == claim_id)
        ).all()
    assert record.decision_type == str(DecisionType.SCRUB)
    assert record.logic_version == "ruleset-v1"
    assert record.input_snapshot["ruleset_digest"] == result.ruleset_digest
    assert record.output["verdict"] == "BLOCK"
    assert record.output["findings"][0]["rule_id"] == "NO_MOD25_ON_EM"
    assert record.output["findings"][0]["rule_version"] == 1
    assert record.output["findings"][0]["line_number"] == 1


def test_the_recorded_inputs_carry_no_phi(root: Path) -> None:
    """PD-13: the facts are recorded in full, so they must hold none."""
    create(rule_doc())
    live("NO_MOD25_ON_EM", 1)
    publish()
    claim_id = load_claim(root)
    with session_scope() as session:
        scrub.scrub_claim(session, claim_id=claim_id, client_id=CLIENT)
        stored = session.scalar(select(Claim).where(Claim.claim_id == claim_id))
        assert stored is not None
        member_id = stored.subscriber_member_id
        record = session.scalar(select(DecisionRecord).where(DecisionRecord.entity_id == claim_id))
        assert record is not None
        dumped = json.dumps(record.input_snapshot)
    assert member_id not in dumped
    assert "member" not in dumped


def test_a_clean_claim_passes(root: Path) -> None:
    create(rule_doc(condition={"field": "line.units", "op": "gt", "value": "50"}))
    live("NO_MOD25_ON_EM", 1)
    publish()
    claim_id = load_claim(root)
    with session_scope() as session:
        result = scrub.scrub_claim(session, claim_id=claim_id, client_id=CLIENT)
    assert result.evaluation.verdict is Verdict.PASS
    assert result.evaluation.findings == ()


def test_another_clients_claim_cannot_be_scrubbed(root: Path) -> None:
    """PD-20 at the query layer."""
    create(rule_doc())
    live("NO_MOD25_ON_EM", 1)
    publish()
    theirs = load_claim(root, "CLM-THEIRS", client=OTHER)
    with session_scope() as session, pytest.raises(ResourceNotFoundError):
        scrub.scrub_claim(session, claim_id=theirs, client_id=CLIENT)
