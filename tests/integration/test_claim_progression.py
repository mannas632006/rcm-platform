"""Claims move through the machine on their own, and a person approves (M3).

Owner decision (2026-11-01): "everything done automatically but visible to
the user who submitted the claim, so that he can review it and approve the
system's steps." ADR-0006.

GATE M3: "created -> scrubbed -> submitted -> acknowledged -> remitted ->
posted or denied. No claim ever rests in a non-terminal state without a next
action date."
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text

from rcm.domain.claim import Claim
from rcm.domain.claim_lifecycle import claims_needing_action, history
from rcm.domain.claim_state import ClaimState
from rcm.domain.claim_state_log import ClaimStateTransition, ReviewStatus
from rcm.ingestion.claim_loader import load_claim_file
from rcm.platform.db import reset_engines, session_scope
from rcm.work import progression
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import WorkItemState
from tests.integration.test_acknowledgement_loading import (
    CLIENT_A,
    ack_277ca,
    acknowledge,
    denial_for,
    payment_for,
    remit,
    submit,
)
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.integration

NOW = datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
TABLES = (
    "submission_outbox, automation_suspension, work_item, claim_state_transition, "
    "claim_acknowledgement, claim_line, claim, remittance_adjustment, "
    "remittance_service_line, claim_payment, provider_adjustment, remittance, raw_file, "
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


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def claim_named(submitter_id: str) -> uuid.UUID:
    with session_scope() as session:
        found = session.scalar(
            select(Claim.claim_id).where(Claim.submitter_claim_id == submitter_id)
        )
        assert found is not None
        return found


def advance() -> progression.ProgressionReport:
    with session_scope() as session:
        return progression.advance_all(session, client_id=CLIENT_A, now=NOW)


def state_of(submitter_id: str) -> ClaimState:
    with session_scope() as session:
        found = session.scalar(select(Claim).where(Claim.submitter_claim_id == submitter_id))
        assert found is not None
        return ClaimState(found.state)


def targets_of(submitter_id: str) -> list[str]:
    with session_scope() as session:
        rows = history(session, claim_id=claim_named(submitter_id), client_id=CLIENT_A)
        return [r.target_state for r in rows]


# ---------------------------------------------------------------------------
# GATE M3's path
# ---------------------------------------------------------------------------


def test_a_loaded_claim_is_walked_to_submitted(root: Path) -> None:
    """An 837 we hold was sent: the system records every step before it."""
    submit("CLM-W", root, control="0001")
    advance()
    assert state_of("CLM-W") is ClaimState.SUBMITTED
    assert targets_of("CLM-W") == [
        "EligibilityVerified",
        "ServiceRendered",
        "Coded",
        "Charged",
        "Scrubbing",
        "ReadyToSubmit",
        "Submitted",
    ]


def test_a_277ca_acceptance_moves_it_into_adjudication(root: Path) -> None:
    submit("CLM-A", root, control="0001")
    acknowledge(ack_277ca(("CLM-A", "A2:20")), root, name="277ca.edi")
    advance()
    assert state_of("CLM-A") is ClaimState.IN_ADJUDICATION


def test_a_277ca_rejection_is_a_rejection_not_a_denial(root: Path) -> None:
    """PD-4 in the state machine."""
    submit("CLM-R", root, control="0001")
    acknowledge(ack_277ca(("CLM-R", "A3:21")), root, name="277ca.edi")
    advance()
    assert state_of("CLM-R") is ClaimState.REJECTED


@pytest.mark.invariant
def test_the_full_path_ends_paid(root: Path) -> None:
    """GATE M3: created -> scrubbed -> submitted -> acknowledged -> remitted
    -> posted."""
    submit("CLM-P", root, control="0001")
    acknowledge(ack_277ca(("CLM-P", "A2:20")), root, name="277ca.edi")
    remit(payment_for("CLM-P", control="0001"), root, name="835.edi")
    advance()
    assert state_of("CLM-P") is ClaimState.PAID


@pytest.mark.invariant
def test_the_full_path_ends_denied(root: Path) -> None:
    submit("CLM-D", root, control="0001")
    acknowledge(ack_277ca(("CLM-D", "A2:20")), root, name="277ca.edi")
    remit(denial_for("CLM-D", control="0001"), root, name="835.edi")
    advance()
    assert state_of("CLM-D") is ClaimState.DENIED


def test_an_835_without_a_277ca_is_inferred_and_says_so(root: Path) -> None:
    """The payer adjudicated it, so it was accepted -- but no 277CA is on
    file, and the history must not claim one was."""
    submit("CLM-I", root, control="0001")
    remit(payment_for("CLM-I", control="0001"), root, name="835.edi")
    advance()
    assert state_of("CLM-I") is ClaimState.PAID
    with session_scope() as session:
        rows = history(session, claim_id=claim_named("CLM-I"), client_id=CLIENT_A)
        accepted = next(r for r in rows if r.target_state == "InAdjudication")
        assert "inferred" in accepted.note


def test_a_resubmission_replaces_the_original(root: Path) -> None:
    """ADR-0006: the denied original ends Replaced; the claim carries on."""
    submit("CLM-O", root, control="0001")
    acknowledge(ack_277ca(("CLM-O", "A2:20")), root, name="277ca.edi")
    remit(denial_for("CLM-O", control="0001"), root, name="835.edi")
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim("CLM-O2", original="CLM-O", frequency="7"), control="0002"),
            client_id=CLIENT_A,
            original_filename="re.edi",
            archive_root=root,
        )
    advance()
    assert state_of("CLM-O") is ClaimState.REPLACED
    assert state_of("CLM-O2") is ClaimState.SUBMITTED
    # The denial is in its history before the replacement, not skipped.
    assert targets_of("CLM-O")[-2:] == ["Denied", "Replaced"]


# ---------------------------------------------------------------------------
# BR-STATE-04 and idempotency
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_no_advanced_claim_rests_without_a_next_action(root: Path) -> None:
    for index, name in enumerate(("CLM-1", "CLM-2", "CLM-3", "CLM-4"), start=1):
        submit(name, root, control=f"000{index}")
    acknowledge(
        ack_277ca(("CLM-2", "A2:20"), ("CLM-3", "A3:21"), ("CLM-4", "A2:20")),
        root,
        name="a.edi",
    )
    remit(payment_for("CLM-4", control="0004"), root, name="835.edi")
    advance()
    with session_scope() as session:
        unscheduled = [
            c
            for c in claims_needing_action(session, client_id=CLIENT_A, as_of=NOW.date())
            if c["unscheduled"]
        ]
    assert unscheduled == []


@pytest.mark.invariant
def test_advancing_twice_changes_nothing(root: Path) -> None:
    submit("CLM-T", root, control="0001")
    acknowledge(ack_277ca(("CLM-T", "A2:20")), root, name="277ca.edi")
    advance()
    with session_scope() as session:
        before = session.scalar(select(func.count()).select_from(ClaimStateTransition))
    report = advance()
    with session_scope() as session:
        after = session.scalar(select(func.count()).select_from(ClaimStateTransition))
    assert before == after
    assert report.transitions == 0


# ---------------------------------------------------------------------------
# The owner's approval step
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_every_system_step_waits_for_the_submitter_to_approve(root: Path) -> None:
    submit("CLM-V", root, control="0001")
    advance()
    with session_scope() as session:
        rows = history(session, claim_id=claim_named("CLM-V"), client_id=CLIENT_A)
        assert rows
        assert all(r.review_status == str(ReviewStatus.PENDING) for r in rows)
        items = session.scalars(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.CLAIM_STEP_REVIEW))
        ).all()
        assert len(items) == 1
        assert items[0].claim_id == claim_named("CLM-V")
        assert items[0].owner_role == "claim_submitter"


def test_approving_marks_every_step_and_closes_the_review(root: Path) -> None:
    submit("CLM-OK", root, control="0001")
    advance()
    claim_id = claim_named("CLM-OK")
    with session_scope() as session:
        progression.approve_steps(
            session, claim_id=claim_id, client_id=CLIENT_A, operator="submitter-1", now=NOW
        )
    with session_scope() as session:
        rows = history(session, claim_id=claim_id, client_id=CLIENT_A)
        assert all(r.review_status == str(ReviewStatus.APPROVED) for r in rows)
        assert all(r.reviewed_by == "submitter-1" for r in rows)
        item = session.scalar(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.CLAIM_STEP_REVIEW))
        )
        assert item is not None
        assert item.state == str(WorkItemState.RESOLVED)


def test_sending_a_step_back_goes_to_a_person(root: Path) -> None:
    submit("CLM-NO", root, control="0001")
    advance()
    claim_id = claim_named("CLM-NO")
    with session_scope() as session:
        step = history(session, claim_id=claim_id, client_id=CLIENT_A)[1]
        step_id = step.transition_id
        progression.send_back_step(
            session,
            transition_id=step_id,
            client_id=CLIENT_A,
            operator="submitter-1",
            reason="Authorisation was required for this service",
            now=NOW,
        )
    with session_scope() as session:
        sent_back = session.get(ClaimStateTransition, step_id)
        assert sent_back is not None
        assert sent_back.review_status == str(ReviewStatus.SENT_BACK)
        assert sent_back.review_note == "Authorisation was required for this service"
        assert (
            session.scalar(select(WorkItem).where(WorkItem.kind == str(WorkItemKind.DATA_QUALITY)))
            is not None
        )


def test_new_steps_after_approval_ask_again(root: Path) -> None:
    """Approval covers the steps that existed; new ones wait for review."""
    submit("CLM-AGAIN", root, control="0001")
    advance()
    claim_id = claim_named("CLM-AGAIN")
    with session_scope() as session:
        progression.approve_steps(
            session, claim_id=claim_id, client_id=CLIENT_A, operator="s", now=NOW
        )
    acknowledge(ack_277ca(("CLM-AGAIN", "A2:20")), root, name="277ca.edi")
    advance()
    with session_scope() as session:
        rows = history(session, claim_id=claim_id, client_id=CLIENT_A)
        assert rows[-1].review_status == str(ReviewStatus.PENDING)
        open_reviews = session.scalars(
            select(WorkItem).where(
                WorkItem.kind == str(WorkItemKind.CLAIM_STEP_REVIEW),
                WorkItem.state == str(WorkItemState.QUEUED),
            )
        ).all()
        assert len(open_reviews) == 1


def test_a_suspended_progression_path_does_not_move_claims(root: Path) -> None:
    """BR-EXC-03: compliance can stop the automation."""
    from rcm.platform.errors import ComplianceError
    from rcm.work.exceptions import AutomationPath, RecordRef, raise_exception

    submit("CLM-S", root, control="0001")
    with session_scope() as session:
        raise_exception(
            session,
            client_id=CLIENT_A,
            kind=WorkItemKind.COMPLIANCE_CONCERN,
            problem="Automatic claim progression is under compliance review",
            record=RecordRef("claim", claim_named("CLM-S")),
            source_key="compliance:progression",
            now=NOW,
            suspends=AutomationPath.CLAIM_PROGRESSION,
        )
    with pytest.raises(ComplianceError):
        advance()
    assert state_of("CLM-S") is ClaimState.REGISTERED
