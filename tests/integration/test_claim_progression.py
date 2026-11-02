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
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
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
    CHARGE,
    CLIENT_A,
    PAID,
    _remittance,
    ack_277ca,
    acknowledge,
    denial_for,
    payment_for,
    remit,
    submit,
)
from tests.integration.test_claim_loading import claim, file_for
from tests.integration.test_reversals import reversal_for

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


def resubmit(claim_id: str, original: str, root: Path, *, frequency: str) -> None:
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim(claim_id, original=original, frequency=frequency), control="0002"),
            client_id=CLIENT_A,
            original_filename=f"{claim_id}.edi",
            archive_root=root,
        )


def problems_on(submitter_id: str) -> list[str]:
    """The DATA_QUALITY problems raised on a claim, by the kind of problem."""
    with session_scope() as session:
        keys = session.scalars(
            select(WorkItem.source_key).where(
                WorkItem.kind == str(WorkItemKind.DATA_QUALITY),
                WorkItem.claim_id == claim_named(submitter_id),
            )
        ).all()
    return sorted(k.rsplit(":", 1)[0] for k in keys)


def test_a_resubmission_replaces_the_original_once_the_payer_takes_it(root: Path) -> None:
    """ADR-0006: the denied original ends Replaced and the claim carries on --
    but only once the payer has taken the replacement. Until then the
    original is the live claim, and ending it would lose it if the
    replacement is rejected."""
    submit("CLM-O", root, control="0001")
    acknowledge(ack_277ca(("CLM-O", "A2:20")), root, name="277ca.edi")
    remit(denial_for("CLM-O", control="0001"), root, name="835.edi")
    resubmit("CLM-O2", "CLM-O", root, frequency="7")
    advance()
    assert state_of("CLM-O") is ClaimState.DENIED
    assert state_of("CLM-O2") is ClaimState.SUBMITTED

    acknowledge(ack_277ca(("CLM-O2", "A2:20"), control="0002"), root, name="277ca-2.edi")
    report = advance()
    assert state_of("CLM-O2") is ClaimState.IN_ADJUDICATION
    assert state_of("CLM-O") is ClaimState.REPLACED
    # The denial is in its history before the replacement, not skipped.
    assert targets_of("CLM-O")[-2:] == ["Denied", "Replaced"]
    # Replacement first, then original: one run was enough.
    assert report.transitions == 2


def test_a_void_is_held_for_a_person_not_called_a_replacement(root: Path) -> None:
    """A void (frequency 8) ends the claim at the payer and may bring a
    recoupment. Until M4 tells it apart, a person handles it."""
    submit("CLM-VO", root, control="0001")
    acknowledge(ack_277ca(("CLM-VO", "A2:20")), root, name="277ca.edi")
    remit(payment_for("CLM-VO", control="0001"), root, name="835.edi")
    resubmit("CLM-VO8", "CLM-VO", root, frequency="8")
    acknowledge(ack_277ca(("CLM-VO8", "A2:20"), control="0002"), root, name="277ca-2.edi")
    advance()
    assert state_of("CLM-VO") is ClaimState.PAID
    assert problems_on("CLM-VO") == ["void-received"]
    # The void itself is no claim to adjudicate: it must not reach Paid or
    # Denied and land in payment or denial work.
    assert state_of("CLM-VO8") is ClaimState.SUBMITTED
    assert problems_on("CLM-VO8") == ["void-submission"]


# ---------------------------------------------------------------------------
# Evidence the module will not guess past (PD-18)
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_rejection_then_an_835_is_held_even_before_the_claim_moved(root: Path) -> None:
    """Both files are on file before the first run. The 835 must not quietly
    win over the rejection, and the history must not say no verdict came."""
    submit("CLM-RX", root, control="0001")
    acknowledge(ack_277ca(("CLM-RX", "A3:21")), root, name="277ca.edi")
    remit(payment_for("CLM-RX", control="0001"), root, name="835.edi")
    advance()
    assert state_of("CLM-RX") is ClaimState.SUBMITTED
    assert problems_on("CLM-RX") == ["rejected-but-adjudicated"]


@pytest.mark.invariant
def test_a_rejection_and_an_835_are_held_even_when_a_replacement_exists(root: Path) -> None:
    """A replacement the payer took does not settle which of the original's
    own files is wrong. The original must not be walked through
    adjudication on a note saying no 277CA verdict came."""
    submit("CLM-RR", root, control="0001")
    acknowledge(ack_277ca(("CLM-RR", "A3:21")), root, name="277ca.edi")
    remit(payment_for("CLM-RR", control="0001"), root, name="835.edi")
    resubmit("CLM-RR2", "CLM-RR", root, frequency="7")
    acknowledge(ack_277ca(("CLM-RR2", "A2:20"), control="0002"), root, name="277ca-2.edi")
    advance()
    assert state_of("CLM-RR2") is ClaimState.IN_ADJUDICATION
    assert state_of("CLM-RR") is ClaimState.SUBMITTED
    assert "InAdjudication" not in targets_of("CLM-RR")
    assert problems_on("CLM-RR") == ["rejected-but-adjudicated"]


def test_a_rejection_after_an_acceptance_goes_to_a_person(root: Path) -> None:
    """The latest 277CA contradicts the state the claim is in. Leaving it in
    adjudication for 30 days says nothing is wrong."""
    submit("CLM-AR", root, control="0001")
    acknowledge(ack_277ca(("CLM-AR", "A2:20")), root, name="277ca.edi")
    advance()
    assert state_of("CLM-AR") is ClaimState.IN_ADJUDICATION
    acknowledge(ack_277ca(("CLM-AR", "A3:21"), control="0002"), root, name="277ca-2.edi")
    advance()
    assert state_of("CLM-AR") is ClaimState.IN_ADJUDICATION
    assert problems_on("CLM-AR") == ["rejected-after-acceptance"]


def test_a_hold_is_a_recorded_decision_written_once(root: Path) -> None:
    """PD-1: why a claim waits is a decision like any step, with its version
    and evidence. Run again, the same hold is not recorded again."""
    from rcm.platform.audit import DecisionRecord

    submit("CLM-HD", root, control="0001")
    acknowledge(ack_277ca(("CLM-HD", "A2:20")), root, name="277ca.edi")
    remit(payment_for("CLM-HD", control="0001"), root, name="835-a.edi")
    remit(denial_for("CLM-HD", control="0002"), root, name="835-b.edi")
    advance()
    advance()
    with session_scope() as session:
        holds = session.scalars(
            select(DecisionRecord).where(
                DecisionRecord.entity_id == claim_named("CLM-HD"),
                DecisionRecord.output["held"].astext == "several-835-postings",
            )
        ).all()
    assert len(holds) == 1
    assert holds[0].logic_version == progression.LOGIC_VERSION
    assert holds[0].input_snapshot["postings_835"] == 2


def test_an_acceptance_after_a_rejection_goes_to_a_person(root: Path) -> None:
    submit("CLM-RA", root, control="0001")
    acknowledge(ack_277ca(("CLM-RA", "A3:21")), root, name="277ca.edi")
    advance()
    assert state_of("CLM-RA") is ClaimState.REJECTED
    acknowledge(ack_277ca(("CLM-RA", "A2:20"), control="0002"), root, name="277ca-2.edi")
    advance()
    assert state_of("CLM-RA") is ClaimState.REJECTED
    assert problems_on("CLM-RA") == ["accepted-after-rejection"]


def test_two_live_835_postings_are_not_picked_between(root: Path) -> None:
    """A split claim or a secondary payer's 835 for the same CLM01: reading
    one and ignoring the other could call a denied claim paid."""
    submit("CLM-2X", root, control="0001")
    acknowledge(ack_277ca(("CLM-2X", "A2:20")), root, name="277ca.edi")
    remit(payment_for("CLM-2X", control="0001"), root, name="835-a.edi")
    remit(denial_for("CLM-2X", control="0002"), root, name="835-b.edi")
    advance()
    assert state_of("CLM-2X") is ClaimState.IN_ADJUDICATION
    assert problems_on("CLM-2X") == ["several-835-postings"]


def test_a_line_paid_nothing_for_the_patient_share_is_not_a_denial(root: Path) -> None:
    """Deductible not met: the payer approved the line and the patient owes
    it all. Calling that a denial queues false denial work."""
    submit("CLM-DED", root, control="0001")
    acknowledge(ack_277ca(("CLM-DED", "A2:20")), root, name="277ca.edi")
    body = [
        f"CLP*CLM-DED*1*{CHARGE}*0.00*{CHARGE}*MB*PCNCLM-DED*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*0.00**1~",
        f"CAS*PR*1*{CHARGE}~",
        "AMT*B6*0.00~",
    ]
    remit(_remittance(body, control="0001", check=Decimal("0.00")), root, name="835.edi")
    advance()
    assert state_of("CLM-DED") is ClaimState.IN_ADJUDICATION
    assert problems_on("CLM-DED") == ["835-patient-share-only-line"]


def test_some_lines_denied_is_partially_paid(root: Path) -> None:
    submit("CLM-PP", root, control="0001")
    acknowledge(ack_277ca(("CLM-PP", "A2:20")), root, name="277ca.edi")
    total = CHARGE * 2
    body = [
        f"CLP*CLM-PP*1*{total}*{PAID}**MB*PCNCLM-PP*11~",
        "NM1*QC*1*Castellanos*Andre****MI*VAZ957419823~",
        f"SVC*HC:99203*{CHARGE}*{PAID}**1~",
        f"CAS*CO*45*{CHARGE - PAID}~",
        f"SVC*HC:99213*{CHARGE}*0.00**1~",
        f"CAS*CO*50*{CHARGE}~",
    ]
    remit(_remittance(body, control="0001", check=PAID), root, name="835.edi")
    advance()
    assert state_of("CLM-PP") is ClaimState.PARTIALLY_PAID
    assert problems_on("CLM-PP") == []


def test_a_reversed_payment_is_not_an_outcome(root: Path) -> None:
    """BR-STATE-03: money moving backwards is a person's call. A payment the
    payer took back says nothing about how the claim ended."""
    submit("CLM-REV", root, control="0001")
    acknowledge(ack_277ca(("CLM-REV", "A2:20")), root, name="277ca.edi")
    remit(payment_for("CLM-REV", control="0001"), root, name="835.edi")
    remit(reversal_for("CLM-REV", control="0002"), root, name="835-rev.edi")
    advance()
    assert state_of("CLM-REV") is ClaimState.IN_ADJUDICATION
    assert "Paid" not in targets_of("CLM-REV")


def test_a_claim_with_a_prior_auth_number_takes_the_auth_path(root: Path) -> None:
    """REF*G1 on the 837: a payer issued an authorisation, so one was needed.
    The steps are recorded as inferred, never as observed."""
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(replace(claim("CLM-AUTH"), prior_auth_number="AUTH12345"), control="0001"),
            client_id=CLIENT_A,
            original_filename="auth.edi",
            archive_root=root,
        )
    advance()
    assert targets_of("CLM-AUTH")[:3] == ["EligibilityVerified", "AuthPending", "AuthApproved"]
    with session_scope() as session:
        rows = history(session, claim_id=claim_named("CLM-AUTH"), client_id=CLIENT_A)
        assert all("inferred" in r.note for r in rows)


def test_evidence_that_arrives_before_the_claim_still_counts(root: Path) -> None:
    """Files arrive out of order. A 277CA and 835 loaded before their 837
    still move the claim once it is loaded -- they are not lost."""
    acknowledge(ack_277ca(("CLM-EARLY", "A2:20")), root, name="277ca.edi")
    remit(denial_for("CLM-EARLY", control="0001"), root, name="835.edi")
    submit("CLM-EARLY", root, control="0001")
    advance()
    assert state_of("CLM-EARLY") is ClaimState.DENIED
    assert targets_of("CLM-EARLY")[-2:] == ["InAdjudication", "Denied"]


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


def step_ids(submitter_id: str) -> list[uuid.UUID]:
    """The steps the submitter has in front of them."""
    with session_scope() as session:
        rows = history(session, claim_id=claim_named(submitter_id), client_id=CLIENT_A)
        return [r.transition_id for r in rows]


def approve(submitter_id: str, steps: list[uuid.UUID], operator: str = "submitter-1") -> int:
    with session_scope() as session:
        return progression.approve_steps(
            session,
            claim_id=claim_named(submitter_id),
            client_id=CLIENT_A,
            step_ids=steps,
            operator=operator,
            now=NOW,
        )


def test_approving_marks_every_step_and_closes_the_review(root: Path) -> None:
    submit("CLM-OK", root, control="0001")
    advance()
    claim_id = claim_named("CLM-OK")
    approve("CLM-OK", step_ids("CLM-OK"))
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
    approve("CLM-AGAIN", step_ids("CLM-AGAIN"), operator="s")
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


@pytest.mark.invariant
def test_approval_covers_only_the_steps_the_submitter_saw(root: Path) -> None:
    """A step taken while the review was open was never seen, so it cannot
    be approved under the submitter's name; the review stays open for it."""
    submit("CLM-SEEN", root, control="0001")
    advance()
    seen = step_ids("CLM-SEEN")
    acknowledge(ack_277ca(("CLM-SEEN", "A2:20")), root, name="277ca.edi")
    advance()
    assert approve("CLM-SEEN", seen) == len(seen)
    with session_scope() as session:
        rows = history(session, claim_id=claim_named("CLM-SEEN"), client_id=CLIENT_A)
        assert rows[-1].target_state == "InAdjudication"
        assert rows[-1].review_status == str(ReviewStatus.PENDING)
        review = session.scalar(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.CLAIM_STEP_REVIEW))
        )
        assert review is not None
        assert review.state != str(WorkItemState.RESOLVED)
    # A retry of the same approval changes nothing (PD-14).
    assert approve("CLM-SEEN", seen) == 0


def test_a_step_of_another_claim_cannot_be_approved_here(root: Path) -> None:
    from rcm.platform.errors import ValidationFailedError

    submit("CLM-X1", root, control="0001")
    submit("CLM-X2", root, control="0002")
    advance()
    with pytest.raises(ValidationFailedError):
        approve("CLM-X1", step_ids("CLM-X2"))


@pytest.mark.invariant
def test_a_sent_back_claim_is_not_moved_whoever_calls(root: Path) -> None:
    """PD-18: the check belongs under the claim lock. A send-back that lands
    between listing claims and advancing one must still stop it."""
    submit("CLM-SB", root, control="0001")
    advance()
    with session_scope() as session:
        progression.send_back_step(
            session,
            transition_id=step_ids("CLM-SB")[0],
            client_id=CLIENT_A,
            operator="submitter-1",
            reason="Eligibility was never checked",
            now=NOW,
        )
    acknowledge(ack_277ca(("CLM-SB", "A2:20")), root, name="277ca.edi")
    with session_scope() as session:
        found = session.get(Claim, claim_named("CLM-SB"))
        assert found is not None
        outcome = progression.advance_claim(session, claim=found, now=NOW)
    assert outcome.held is True
    assert state_of("CLM-SB") is ClaimState.SUBMITTED


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
