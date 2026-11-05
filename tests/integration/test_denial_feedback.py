"""The denial feedback loop (M4.9, SRS 8.4, FR-RULES-07, ADR-0013).

"Every preventable denial produces either a candidate rule or a recorded
decision not to create one." Inject a denial; a candidate rule appears,
backtested and waiting for a person -- never promoted by the loop itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from rcm.decision import feedback
from rcm.decision.feedback_models import DenialFeedback
from rcm.decision.rules import lifecycle
from rcm.decision.rules.definition import RuleStatus
from rcm.decision.rules.models import RuleBacktest, RuleVersion
from rcm.decision.rules.scrub import scrub_claim
from rcm.decision.rules.store import publish_ruleset
from rcm.domain.claim import Claim
from rcm.domain.remittance import ClaimPayment
from rcm.platform.audit import DecisionRecord, DecisionType
from rcm.platform.db import reset_engines, session_scope
from rcm.work import denials as work_denials
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_acknowledgement_loading import (
    CHARGE,
    CLIENT_A,
    CLIENT_B,
    payment_for,
    remit,
    submit,
)
from tests.integration.test_denial_classification import one_line
from tests.integration.test_rule_backtest import ALL

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE denial_feedback, denial_classification, rule_status_change, "
                    f"{ALL} RESTART IDENTITY CASCADE"
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


def denied(claim_id: str, carc: str, root: Path, *, control: str, client: uuid.UUID = CLIENT_A):
    submit(claim_id, root, client=client, control=control)
    remit(
        one_line(claim_id, f"CAS*CO*{carc}*{CHARGE}~", control=control),
        root,
        client=client,
        name=f"{claim_id}.835",
    )


def route(client: uuid.UUID = CLIENT_A) -> None:
    with session_scope() as session:
        work_denials.classify_and_route(session, client_id=client, now=NOW)


def outcomes() -> dict[str, str]:
    """Outcome per denied claim's CLM01."""
    with session_scope() as session:
        rows = session.execute(
            select(ClaimPayment.claim_id, DenialFeedback.outcome).join(
                ClaimPayment,
                ClaimPayment.claim_payment_id == DenialFeedback.claim_payment_id,
            )
        )
        return {str(claim_id): str(outcome) for claim_id, outcome in rows}


def candidates() -> list[RuleVersion]:
    with session_scope() as session:
        return list(session.scalars(select(RuleVersion).order_by(RuleVersion.rule_id)).all())


@pytest.mark.invariant
def test_a_preventable_denial_drafts_a_candidate_rule(root: Path) -> None:
    """M4.9 acceptance: inject a denial, a candidate rule appears."""
    denied("CLM-AU", "197", root, control="0001")
    route()
    (rule,) = candidates()
    assert rule.rule_id.startswith("FB_AUTH_")
    assert rule.document["provenance"]["source"] == "denial_feedback"
    # Drafted and backtested by the loop; promoted only by a person.
    assert rule.status == str(RuleStatus.BACKTESTING)
    with session_scope() as session:
        (backtest,) = session.scalars(select(RuleBacktest)).all()
    assert (backtest.rule_id, backtest.version, backtest.client_id) == (rule.rule_id, 1, CLIENT_A)
    assert backtest.true_positives == 1
    assert outcomes() == {"CLM-AU": str(feedback.FeedbackOutcome.CANDIDATE_DRAFTED)}


def test_the_same_pattern_again_joins_its_candidate(root: Path) -> None:
    denied("CLM-AU1", "197", root, control="0001")
    route()
    denied("CLM-AU2", "198", root, control="0002")
    route()
    (rule,) = candidates()
    assert outcomes() == {
        "CLM-AU1": str(feedback.FeedbackOutcome.CANDIDATE_DRAFTED),
        "CLM-AU2": str(feedback.FeedbackOutcome.CANDIDATE_EXISTS),
    }
    with session_scope() as session:
        rows = session.scalars(select(DenialFeedback)).all()
    assert {(r.rule_id, r.rule_version) for r in rows} == {(rule.rule_id, 1)}


def test_a_not_preventable_denial_is_logged_not_ruled(root: Path) -> None:
    """SRS 8.4: not preventable -> log environmental."""
    denied("CLM-NC", "96", root, control="0001")
    route()
    assert candidates() == []
    assert outcomes() == {"CLM-NC": str(feedback.FeedbackOutcome.NOT_PREVENTABLE)}


@pytest.mark.invariant
def test_every_preventable_denial_gets_a_rule_or_a_reason(root: Path) -> None:
    """FR-RULES-07 / BR-M7-01, across categories a rule can and cannot catch."""
    denied("CLM-AU", "197", root, control="0001")  # authorisation: a rule
    denied("CLM-MN", "50", root, control="0002")  # medical necessity: a rule
    denied("CLM-TF", "29", root, control="0003")  # timely filing: no claim field
    route()
    got = outcomes()
    assert got == {
        "CLM-AU": str(feedback.FeedbackOutcome.CANDIDATE_DRAFTED),
        "CLM-MN": str(feedback.FeedbackOutcome.CANDIDATE_DRAFTED),
        "CLM-TF": str(feedback.FeedbackOutcome.NO_RULE),
    }
    with session_scope() as session:
        (no_rule,) = session.scalars(
            select(DenialFeedback)
            .join(ClaimPayment, ClaimPayment.claim_payment_id == DenialFeedback.claim_payment_id)
            .where(ClaimPayment.claim_id == "CLM-TF")
        ).all()
        records = session.scalars(
            select(DecisionRecord).where(
                DecisionRecord.decision_type == str(DecisionType.DENIAL_FEEDBACK),
                DecisionRecord.entity_id == no_rule.claim_payment_id,
            )
        ).all()
    assert no_rule.rule_id is None
    assert no_rule.reason
    (record,) = records
    assert record.output["lines"][0]["outcome"] == str(feedback.FeedbackOutcome.NO_RULE)


@pytest.mark.invariant
def test_a_denial_a_live_rule_flagged_is_a_defect(root: Path) -> None:
    """SRS 8.4: an existing rule covered it -> DEFECT, rule failed. Put before
    the rules reviewer, never drafted over."""
    # A paid claim on file, so the rule's backtest has an outcome to score.
    submit("CLM-PD", root, control="0002")
    remit(payment_for("CLM-PD", control="0002"), root, name="pd.835")
    submit("CLM-AU", root, control="0001")
    with session_scope() as session:
        claim_id, payer_id = session.execute(
            select(Claim.claim_id, Claim.payer_id).where(Claim.submitter_claim_id == "CLM-AU")
        ).one()
    drafted = feedback.candidate_for(
        feedback.DenialCategory.AUTHORISATION, payer_id=payer_id, cpt="99203", carc="197"
    )
    assert isinstance(drafted, feedback.Draft)
    document = {**drafted.document, "rule_id": "AUTH_99203_HAND_WRITTEN"}
    _make_live(document)
    with session_scope() as session:
        scrub_claim(session, claim_id=claim_id, client_id=CLIENT_A)
    remit(one_line("CLM-AU", f"CAS*CO*197*{CHARGE}~", control="0001"), root, name="au.835")
    route()
    assert outcomes() == {"CLM-AU": str(feedback.FeedbackOutcome.RULE_FAILED)}
    assert [r.rule_id for r in candidates()] == ["AUTH_99203_HAND_WRITTEN"]
    with session_scope() as session:
        (item,) = session.scalars(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.RULE_CONFLICT))
        ).all()
    assert "AUTH_99203_HAND_WRITTEN" in item.title


@pytest.mark.invariant
def test_running_the_loop_again_changes_nothing(root: Path) -> None:
    denied("CLM-AU", "197", root, control="0001")
    route()
    route()
    assert len(candidates()) == 1
    assert len(outcomes()) == 1
    with session_scope() as session:
        assert len(session.scalars(select(RuleBacktest)).all()) == 1


def test_the_loop_reads_only_its_clients_denials(root: Path) -> None:
    """PD-20."""
    denied("CLM-A", "197", root, control="0001")
    denied("CLM-B", "197", root, control="0002", client=CLIENT_B)
    route(CLIENT_A)
    assert outcomes() == {"CLM-A": str(feedback.FeedbackOutcome.CANDIDATE_DRAFTED)}
    with session_scope() as session:
        assert {r.client_id for r in session.scalars(select(DenialFeedback))} == {CLIENT_A}


@pytest.mark.invariant
def test_an_untraced_denial_is_put_before_a_person(root: Path) -> None:
    """PD-18: no 837 on file, so the loop cannot learn from it. Not a quiet
    row: a named exception."""
    remit(one_line("CLM-GHOST", f"CAS*CO*197*{CHARGE}~", control="0001"), root, name="g.835")
    route()
    route()
    assert outcomes() == {"CLM-GHOST": str(feedback.FeedbackOutcome.NO_CLAIM)}
    with session_scope() as session:
        (item,) = session.scalars(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.DATA_QUALITY))
        ).all()
    assert "No submitted claim" in item.title


def test_a_shadow_rule_covers_nothing(root: Path) -> None:
    """A shadow rule never acts on a claim; the pattern still gets its
    candidate."""
    submit("CLM-PD", root, control="0002")
    remit(payment_for("CLM-PD", control="0002"), root, name="pd.835")
    _live_rule("AUTH_SHADOW", feedback.DenialCategory.AUTHORISATION, to=RuleStatus.SHADOW)
    denied("CLM-AU", "197", root, control="0001")
    route()
    assert outcomes()["CLM-AU"] == str(feedback.FeedbackOutcome.CANDIDATE_DRAFTED)


def test_a_flag_for_another_cause_is_not_a_failed_rule(root: Path) -> None:
    """A coding rule on the line says nothing about an authorisation denial."""
    submit("CLM-PD", root, control="0002")
    remit(payment_for("CLM-PD", control="0002"), root, name="pd.835")
    _live_rule("CODING_ADVISORY", feedback.DenialCategory.CODING)
    submit("CLM-AU", root, control="0001")
    with session_scope() as session:
        claim_id = session.scalars(
            select(Claim.claim_id).where(Claim.submitter_claim_id == "CLM-AU")
        ).one()
        scrub_claim(session, claim_id=claim_id, client_id=CLIENT_A)
    remit(one_line("CLM-AU", f"CAS*CO*197*{CHARGE}~", control="0001"), root, name="au.835")
    route()
    assert outcomes()["CLM-AU"] == str(feedback.FeedbackOutcome.CANDIDATE_DRAFTED)


def _live_rule(
    rule_id: str, category: feedback.DenialCategory, *, to: RuleStatus = RuleStatus.ADVISORY
) -> None:
    """A rule declaring `category`, scoped to the fixture's payer and CPT."""
    with session_scope() as session:
        payer_id = session.scalars(select(Claim.payer_id).limit(1)).one()
    drafted = feedback.candidate_for(category, payer_id=payer_id, cpt="99203", carc="0")
    assert isinstance(drafted, feedback.Draft)
    _make_live({**drafted.document, "rule_id": rule_id}, to=to)


def _make_live(document: dict[str, object], *, to: RuleStatus = RuleStatus.ADVISORY) -> None:
    """A hand-written rule taken through the lifecycle to `to`, published."""
    from rcm.decision.rules.backtest import run_backtest
    from rcm.decision.rules.store import create_rule_version

    actor = "test:rules_reviewer"
    with session_scope() as session:
        rule_id = str(document["rule_id"])
        version = create_rule_version(session, document=document, created_by=actor)
        lifecycle.change_status(
            session,
            rule_id=rule_id,
            version=version,
            to=RuleStatus.BACKTESTING,
            changed_by=actor,
            reason="test",
        )
        run_backtest(session, rule_id=rule_id, version=version, client_id=CLIENT_A, ran_by=actor)
        steps = (RuleStatus.SHADOW, RuleStatus.ADVISORY)
        for step in steps[: steps.index(to) + 1]:
            lifecycle.change_status(
                session,
                rule_id=rule_id,
                version=version,
                to=step,
                changed_by=actor,
                reason="test",
            )
        publish_ruleset(session, published_by=actor)
