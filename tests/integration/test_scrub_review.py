"""Scrubber exceptions on claims already sent (M5.8, ADR-0019).

- each blocking finding is one named item: rule, version, line, message
  (BR-EXC-01), routed where the rule says;
- the review never changes the claim: it is already with the payer;
- once per claim per ruleset (idempotent), again under a new ruleset;
- no ruleset is an error, never a clean bill of health (PD-18).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text

from rcm.decision.rules import scrub
from rcm.decision.rules.definition import RuleStatus
from rcm.domain.claim import Claim
from rcm.domain.claim_state import ClaimState
from rcm.platform.audit import DecisionRecord, DecisionType
from rcm.platform.auth import Principal, Role
from rcm.platform.db import session_scope
from rcm.platform.errors import ResourceNotFoundError
from rcm.work import claim_detail, scrub_review
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_acknowledgement_loading import (
    CLIENT_A,
    ack_277ca,
    acknowledge,
    submit,
)
from tests.integration.test_api_shell import clean  # noqa: F401
from tests.integration.test_auto_correction_scrub import dotted_rule
from tests.integration.test_claim_progression import advance
from tests.integration.test_rules_engine import create, live, publish, rule_doc

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
_RULE_TABLES = "ruleset_member, ruleset, rule_version"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


@pytest.fixture(autouse=True)
def rules(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {_RULE_TABLES} RESTART IDENTITY CASCADE"))

    wipe()
    yield
    wipe()


def sent(root: Path, submitter_id: str = "CLM-SR", *, accepted: bool = True) -> None:
    """A claim the PM sent, and the payer took (InAdjudication) or not yet
    (Submitted)."""
    submit(submitter_id, root, control="0001")
    if accepted:
        acknowledge(ack_277ca((submitter_id, "A2:20")), root, name=f"{submitter_id}.277ca")
    advance()


def review() -> scrub_review.ScrubReviewReport:
    with session_scope() as session:
        return scrub_review.review_client(session, client_id=CLIENT_A, now=NOW)


def items(kind: WorkItemKind = WorkItemKind.SCRUB_EXCEPTION) -> list[WorkItem]:
    with session_scope() as session:
        return list(session.scalars(select(WorkItem).where(WorkItem.kind == str(kind))).all())


def claim_id(submitter_id: str = "CLM-SR") -> uuid.UUID:
    with session_scope() as session:
        found = session.scalar(
            select(Claim.claim_id).where(Claim.submitter_claim_id == submitter_id)
        )
    assert found is not None
    return found


def scrubs(submitter_id: str = "CLM-SR") -> int:
    """This claim's scrub records. decision_record is append-only and is not
    wiped between tests, so the count is always per claim."""
    with session_scope() as session:
        count = session.scalar(
            select(func.count())
            .select_from(DecisionRecord)
            .where(
                DecisionRecord.decision_type == str(DecisionType.SCRUB),
                DecisionRecord.entity_id == claim_id(submitter_id),
            )
        )
    return int(count or 0)


def blocking(document: dict[str, Any]) -> None:
    live(str(document["rule_id"]), create(document))
    publish()


@pytest.mark.invariant
def test_a_blocking_finding_is_one_named_item(root: Path) -> None:
    """BR-EXC-01: the rule, its version, the line and its message -- never
    "scrub failed"."""
    sent(root)
    blocking(rule_doc())
    report = review()
    assert (report.claims_reviewed, report.claims_blocked, report.exceptions_raised) == (1, 1, 1)
    (item,) = items()
    assert item.title.startswith("Scrubber rule NO_MOD25_ON_EM v1 blocks line 1: ")
    assert "modifier 25" in item.title
    assert item.owner_role == "billing_specialist"
    with session_scope() as session:
        assert item.claim_id == session.scalar(
            select(Claim.claim_id).where(Claim.submitter_claim_id == "CLM-SR")
        )


@pytest.mark.invariant
def test_the_review_never_changes_a_sent_claim(root: Path) -> None:
    """A fix applied to a sent claim would make the row disagree with the 837
    the payer holds (PD-9). The finding is raised; the claim keeps its dot."""
    sent(root)
    blocking(dotted_rule())
    review()
    with session_scope() as session:
        stored = session.scalar(select(Claim).where(Claim.submitter_claim_id == "CLM-SR"))
        record = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.decision_type == str(DecisionType.SCRUB),
                DecisionRecord.entity_id == claim_id(),
            )
        )
    assert stored is not None and "J06.9" in stored.diagnoses
    assert record is not None and record.input_snapshot["mode"] == "review"
    assert record.output["corrections"] == []
    (item,) = items()
    assert "the claim" in item.title


def test_a_second_run_raises_and_records_nothing(root: Path) -> None:
    sent(root)
    blocking(rule_doc())
    review()
    again = review()
    assert again.claims_reviewed == 0
    assert len(items()) == 1
    assert scrubs() == 1


def test_a_new_ruleset_reviews_the_claim_again(root: Path) -> None:
    sent(root)
    blocking(rule_doc())
    review()
    publish()
    assert review().claims_reviewed == 1
    assert scrubs() == 2
    # The same finding is the same open item, not a second one.
    assert len(items()) == 1


def test_an_advisory_finding_raises_nothing(root: Path) -> None:
    sent(root)
    live("NO_MOD25_ON_EM", create(rule_doc()), RuleStatus.ADVISORY)
    publish()
    report = review()
    assert (report.claims_reviewed, report.exceptions_raised) == (1, 0)
    assert items() == []


def test_a_rule_routes_its_finding(root: Path) -> None:
    sent(root)
    blocking(
        rule_doc(
            action={
                "type": "FLAG",
                "message": "Check eligibility for this E/M line",
                "route_to": "ELIGIBILITY_PROBLEM",
            }
        )
    )
    review()
    assert items() == []
    (item,) = items(WorkItemKind.ELIGIBILITY_PROBLEM)
    assert "Check eligibility" in item.title


def test_a_claim_not_yet_accepted_is_reviewed_too(root: Path) -> None:
    """Submitted counts: the payer has not decided, so a fix can still land."""
    sent(root, "CLM-WAIT", accepted=False)
    blocking(rule_doc())
    with session_scope() as session:
        state = session.scalar(select(Claim.state).where(Claim.submitter_claim_id == "CLM-WAIT"))
    assert state == str(ClaimState.SUBMITTED)
    assert review().claims_reviewed == 1


@pytest.mark.invariant
def test_no_ruleset_is_an_error_not_a_pass(root: Path) -> None:
    """PD-18: no rules is not "no problems"."""
    sent(root)
    with pytest.raises(ResourceNotFoundError):
        review()


def test_the_finding_is_on_the_claims_detail(root: Path) -> None:
    """S-03: a person on the claim sees what is waiting on it, by name."""
    sent(root)
    blocking(rule_doc())
    review()
    reader = Principal(
        subject="ar.amy", roles=frozenset({Role.AR_SPECIALIST}), client_ids=frozenset({CLIENT_A})
    )
    with session_scope() as session:
        detail = claim_detail.claim_detail(
            session, principal=reader, claim_id=claim_id(), today=NOW.date()
        )
    scrubber = [p for p in detail.open_problems if p.kind == str(WorkItemKind.SCRUB_EXCEPTION)]
    (problem,) = scrubber
    assert problem.title.startswith("Scrubber rule NO_MOD25_ON_EM v1 blocks line 1")
    assert problem.detail == scrub_review.DETAIL


def test_a_correct_mode_scrub_does_not_count_as_a_review(root: Path) -> None:
    """A correct-mode scrub (ADR-0018) raises nothing, so the same ruleset
    must still review the claim and raise its finding."""
    sent(root)
    blocking(rule_doc())
    with session_scope() as session:
        scrub.scrub_claim(session, claim_id=claim_id(), client_id=CLIENT_A, today=NOW.date())
    assert review().claims_reviewed == 1
    assert len(items()) == 1
