"""A scrub's corrections land on the stored claim, audited and recorded
(M4.7, ADR-0011 Decision 4)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, text

from rcm.decision.rules import scrub
from rcm.decision.rules.definition import RuleStatus
from rcm.decision.rules.evaluate import Verdict
from rcm.domain.claim import Claim
from rcm.platform.audit import AuditEvent, DecisionRecord
from rcm.platform.db import reset_engines, session_scope
from tests.integration.test_rules_engine import (
    CLIENT,
    TABLES,
    create,
    live,
    load_claim,
    publish,
)

pytestmark = pytest.mark.integration


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
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def dotted_rule(**action: Any) -> dict[str, Any]:
    act: dict[str, Any] = {
        "type": "FLAG",
        "message": "Diagnosis codes go without a decimal point",
        "route_to": "SCRUB_EXCEPTION",
        "auto_correct": True,
        "fix": "STRIP_DIAGNOSIS_DECIMAL",
    }
    act.update(action)
    act = {k: v for k, v in act.items() if v is not None}
    return {
        "rule_id": "NO_DOTS",
        "layer": "STRUCTURAL",
        "severity": "BLOCKING",
        "condition": {"field": "claim.diagnoses", "op": "contains", "value": "J06.9"},
        "action": act,
    }


def stored_diagnoses(claim_id: object) -> list[str]:
    with session_scope() as session:
        found = session.scalar(select(Claim).where(Claim.claim_id == claim_id))
        assert found is not None
        return list(found.diagnoses)


@pytest.mark.invariant
def test_a_correction_lands_on_the_claim_audited_and_recorded(root: Path) -> None:
    create(dotted_rule())
    live("NO_DOTS", 1)
    publish()
    claim_id = load_claim(root)
    assert stored_diagnoses(claim_id) == ["J06.9", "E11.65"]

    with session_scope() as session:
        result = scrub.scrub_claim(session, claim_id=claim_id, client_id=CLIENT)
    assert result.evaluation.verdict is Verdict.PASS
    assert [c.fix for c in result.corrections] == ["STRIP_DIAGNOSIS_DECIMAL"]
    assert stored_diagnoses(claim_id) == ["J069", "E1165"]

    with session_scope() as session:
        (audit,) = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == claim_id, AuditEvent.action == "claim.auto_corrected"
            )
        ).all()
        record = session.scalar(select(DecisionRecord).where(DecisionRecord.entity_id == claim_id))
        assert record is not None
    # PD-2 in the audit, without the codes; PD-1 in the record, with them.
    assert audit.after_state == {
        "field": "claim.diagnoses",
        "line_number": None,
        "fix": "STRIP_DIAGNOSIS_DECIMAL",
    }
    assert "J06" not in json.dumps(audit.after_state)
    assert record.output["corrections"] == [
        {
            "fix": "STRIP_DIAGNOSIS_DECIMAL",
            "field": "claim.diagnoses",
            "line_number": None,
            "before": ["J06.9", "E11.65"],
            "after": ["J069", "E1165"],
        }
    ]
    assert record.output["verdict"] == "PASS"


def test_a_claim_that_blocks_without_a_fix_is_left_as_it_was(root: Path) -> None:
    create(dotted_rule(auto_correct=False, fix=None))
    live("NO_DOTS", 1)
    publish()
    claim_id = load_claim(root)
    with session_scope() as session:
        result = scrub.scrub_claim(session, claim_id=claim_id, client_id=CLIENT)
    assert result.evaluation.verdict is Verdict.BLOCK
    assert result.corrections == ()
    assert stored_diagnoses(claim_id) == ["J06.9", "E11.65"]


def test_a_shadow_rule_never_changes_a_claim(root: Path) -> None:
    create(dotted_rule())
    live("NO_DOTS", 1, RuleStatus.SHADOW)
    publish()
    claim_id = load_claim(root)
    with session_scope() as session:
        scrub.scrub_claim(session, claim_id=claim_id, client_id=CLIENT)
    assert stored_diagnoses(claim_id) == ["J06.9", "E11.65"]


@pytest.mark.invariant
def test_the_claim_writer_refuses_to_add_modifier_59(root: Path) -> None:
    """PD-16 at the last step: whatever asks, the stored line never gains
    25, 59 or X{EPSU} from an automatic correction."""
    from rcm.domain import claim_correction
    from rcm.platform.errors import ManualCodingRequiredError

    claim_id = load_claim(root)
    with session_scope() as session, pytest.raises(ManualCodingRequiredError):
        claim_correction.correct_line_modifiers(
            session,
            claim_id=claim_id,
            client_id=CLIENT,
            line_number=1,
            modifiers=["59"],
            fix="UPPERCASE_MODIFIERS",
            actor="rules_engine:scrubber",
        )


def test_a_diagnosis_correction_cannot_add_or_drop_a_code(root: Path) -> None:
    """Line pointers index the diagnoses: a correction rewrites, never
    reshapes."""
    from rcm.domain import claim_correction
    from rcm.platform.errors import ValidationFailedError

    claim_id = load_claim(root)
    with session_scope() as session, pytest.raises(ValidationFailedError):
        claim_correction.correct_diagnoses(
            session,
            claim_id=claim_id,
            client_id=CLIENT,
            diagnoses=["J069"],
            fix="STRIP_DIAGNOSIS_DECIMAL",
            actor="rules_engine:scrubber",
        )
    assert stored_diagnoses(claim_id) == ["J06.9", "E11.65"]
