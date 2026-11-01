"""The submission outbox: send exactly once, lose nothing (M3.7).

IR-06 (MUST): Submission MUST use the transactional outbox pattern.
Writing "submitted" to the database and transmitting the file must not
be able to diverge.
IR-07 (MUST): Every submission carries an idempotency key. Retries MUST
NOT create duplicate claims (EC-41).
IR-09 (MUST): A circuit breaker MUST trigger failover.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from rcm.domain.claim import Claim
from rcm.domain.claim_lifecycle import current_state, transition
from rcm.domain.claim_state import ClaimState, Guard
from rcm.ingestion.claim_loader import load_claim_file
from rcm.platform.audit import ActorType
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ComplianceError, StateTransitionError
from rcm.submission import outbox
from rcm.submission.adapters import MockAdapter
from rcm.submission.models import OutboxStatus, SubmissionOutbox
from rcm.submission.router import ClearinghouseRouter
from rcm.work.exceptions import AutomationPath, RecordRef, raise_exception
from rcm.work.models import WorkItem, WorkItemKind
from tests.integration.test_claim_loading import claim, file_for

pytestmark = pytest.mark.integration

CLIENT = uuid.UUID("7a7a7a7a-0000-4000-8000-00000000007a")
OTHER_CLIENT = uuid.UUID("7b7b7b7b-0000-4000-8000-00000000007b")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
PAYLOAD = b"ISA*00*          *00*          *ZZ*SUBMITTER01~"

TABLES = (
    "submission_outbox, automation_suspension, work_item, claim_state_transition, "
    "claim_acknowledgement, claim_line, claim, remittance_adjustment, "
    "remittance_service_line, claim_payment, provider_adjustment, remittance, raw_file, "
    "payer_alias, payer, payer_match_candidate"
)

#: Registered -> ReadyToSubmit, the documented path with its guard answered.
PATH_TO_READY = (
    (ClaimState.ELIGIBILITY_VERIFIED, None),
    (ClaimState.SERVICE_RENDERED, {str(Guard.AUTH_NOT_REQUIRED): True}),
    (ClaimState.CODED, None),
    (ClaimState.CHARGED, None),
    (ClaimState.SCRUBBING, None),
    (ClaimState.READY_TO_SUBMIT, None),
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
def ready_claim(tmp_path: Path) -> uuid.UUID:
    root = tmp_path / "raw"
    root.mkdir()
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim("CLM-OUTBOX")),
            client_id=CLIENT,
            original_filename="claims.edi",
            archive_root=root,
        )
    with session_scope() as session:
        claim_id = session.scalars(select(Claim.claim_id).where(Claim.client_id == CLIENT)).one()
        for target, facts in PATH_TO_READY:
            transition(
                session,
                claim_id=claim_id,
                client_id=CLIENT,
                target=target,
                actor_type=ActorType.SYSTEM,
                actor_id="system:test",
                facts=facts,
                next_action_due=date(2026, 6, 2),
            )
    return claim_id


def enqueue(claim_id: uuid.UUID, *, key: str = "submit-1", client_id: uuid.UUID = CLIENT) -> None:
    with session_scope() as session:
        outbox.enqueue(
            session,
            claim_id=claim_id,
            client_id=client_id,
            payload=PAYLOAD,
            idempotency_key=key,
            actor_id="system:test",
            now=NOW,
        )


def rows() -> list[SubmissionOutbox]:
    with session_scope() as session:
        found = list(session.scalars(select(SubmissionOutbox)).all())
        for row in found:
            session.expunge(row)
        return found


def state_of(claim_id: uuid.UUID) -> ClaimState:
    with session_scope() as session:
        return current_state(session, claim_id=claim_id, client_id=CLIENT)


def router(primary: MockAdapter, secondary: MockAdapter) -> ClearinghouseRouter:
    return ClearinghouseRouter(
        primary=primary, secondary=secondary, failure_threshold=2, open_seconds=60
    )


def dispatch(r: ClearinghouseRouter, now: datetime = NOW) -> outbox.DispatchReport:
    with session_scope() as session:
        return outbox.dispatch(session, router=r, client_id=CLIENT, now=now)


# ---------------------------------------------------------------------------
# IR-06: the state change and the outbox row are one write
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_enqueue_marks_submitted_and_writes_the_outbox_together(ready_claim: uuid.UUID) -> None:
    enqueue(ready_claim)
    assert state_of(ready_claim) is ClaimState.SUBMITTED
    (row,) = rows()
    assert row.status == str(OutboxStatus.PENDING)
    assert row.claim_id == ready_claim


def test_a_rolled_back_enqueue_leaves_neither(ready_claim: uuid.UUID) -> None:
    """IR-06: there is no state in which one exists without the other."""
    with pytest.raises(RuntimeError), session_scope() as session:
        outbox.enqueue(
            session,
            claim_id=ready_claim,
            client_id=CLIENT,
            payload=PAYLOAD,
            idempotency_key="submit-rollback",
            actor_id="system:test",
            now=NOW,
        )
        raise RuntimeError("caller failed after enqueue")
    assert state_of(ready_claim) is ClaimState.READY_TO_SUBMIT
    assert rows() == []


def test_only_a_claim_ready_to_submit_can_be_enqueued(ready_claim: uuid.UUID) -> None:
    enqueue(ready_claim)
    with pytest.raises(StateTransitionError):
        enqueue(ready_claim, key="submit-2")


# ---------------------------------------------------------------------------
# IR-07 / EC-41: a retry never duplicates
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_retried_enqueue_with_the_same_key_is_one_submission(ready_claim: uuid.UUID) -> None:
    enqueue(ready_claim)
    enqueue(ready_claim)  # the caller's retry: same key
    assert len(rows()) == 1


@pytest.mark.invariant
def test_a_sent_row_is_never_sent_again(ready_claim: uuid.UUID) -> None:
    primary, secondary = MockAdapter("primary"), MockAdapter("secondary")
    r = router(primary, secondary)
    enqueue(ready_claim)
    assert dispatch(r).sent == 1
    assert dispatch(r, NOW + timedelta(hours=1)).sent == 0
    assert primary.attempts == 1


# ---------------------------------------------------------------------------
# IR-09: failover, and no data loss when both are down
# ---------------------------------------------------------------------------


def test_dispatch_sends_through_the_primary(ready_claim: uuid.UUID) -> None:
    primary, secondary = MockAdapter("primary"), MockAdapter("secondary")
    enqueue(ready_claim)
    report = dispatch(router(primary, secondary))
    assert (report.sent, report.deferred) == (1, 0)
    (row,) = rows()
    assert row.status == str(OutboxStatus.SENT)
    assert row.sent_via == "primary"
    assert row.receipt_id


def test_a_failed_primary_sends_through_the_secondary(ready_claim: uuid.UUID) -> None:
    primary, secondary = MockAdapter("primary", failing=True), MockAdapter("secondary")
    enqueue(ready_claim)
    dispatch(router(primary, secondary))
    (row,) = rows()
    assert row.sent_via == "secondary"


@pytest.mark.invariant
def test_both_down_loses_nothing_and_recovers(ready_claim: uuid.UUID) -> None:
    """IR-09 + IR-06: no route means the row waits, a person is told, and
    the file goes exactly once when a clearinghouse is back."""
    primary = MockAdapter("primary", failing=True)
    secondary = MockAdapter("secondary", failing=True)
    r = router(primary, secondary)
    enqueue(ready_claim)

    report = dispatch(r)
    assert (report.sent, report.deferred) == (0, 1)
    (row,) = rows()
    assert row.status == str(OutboxStatus.PENDING)
    assert row.attempts == 1
    assert row.last_error == "external-system-unavailable"
    with session_scope() as session:
        outage = session.scalars(
            select(WorkItem).where(WorkItem.kind == str(WorkItemKind.EXTERNAL_SYSTEM_FAILURE))
        ).all()
        assert len(outage) == 1

    # Not retried before its next attempt time.
    assert dispatch(r, NOW + timedelta(seconds=1)).deferred == 0

    secondary.failing = False
    later = NOW + timedelta(hours=1)
    assert dispatch(r, later).sent == 1
    assert secondary.transmissions == 1
    (row,) = rows()
    assert row.status == str(OutboxStatus.SENT)


# ---------------------------------------------------------------------------
# BR-EXC-03 and PD-20
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_a_suspended_submission_path_refuses_to_enqueue(ready_claim: uuid.UUID) -> None:
    with session_scope() as session:
        raise_exception(
            session,
            client_id=CLIENT,
            kind=WorkItemKind.COMPLIANCE_CONCERN,
            problem="Submissions to this payer are under compliance review",
            record=RecordRef("claim", ready_claim),
            source_key="compliance:submit",
            now=NOW,
            suspends=AutomationPath.CLAIM_SUBMISSION,
        )
    with pytest.raises(ComplianceError):
        enqueue(ready_claim)
    assert state_of(ready_claim) is ClaimState.READY_TO_SUBMIT
    assert rows() == []


def test_a_suspended_path_does_not_dispatch(ready_claim: uuid.UUID) -> None:
    """A suspension raised after enqueue still stops the file leaving."""
    primary = MockAdapter("primary")
    enqueue(ready_claim)
    with session_scope() as session:
        raise_exception(
            session,
            client_id=CLIENT,
            kind=WorkItemKind.COMPLIANCE_CONCERN,
            problem="Submissions to this payer are under compliance review",
            record=RecordRef("claim", ready_claim),
            source_key="compliance:dispatch",
            now=NOW,
            suspends=AutomationPath.CLAIM_SUBMISSION,
        )
    with pytest.raises(ComplianceError):
        dispatch(router(primary, MockAdapter("secondary")))
    assert primary.attempts == 0


def test_another_clients_claim_cannot_be_enqueued(ready_claim: uuid.UUID) -> None:
    from rcm.platform.errors import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        enqueue(ready_claim, client_id=OTHER_CLIENT)


# ---------------------------------------------------------------------------
# Review findings: every outcome lands somewhere a person can see
# ---------------------------------------------------------------------------


@pytest.fixture
def two_ready_claims(tmp_path: Path) -> tuple[uuid.UUID, uuid.UUID]:
    root = tmp_path / "raw2"
    root.mkdir()
    with session_scope() as session:
        load_claim_file(
            session,
            file_for(claim("CLM-A1"), claim("CLM-B2")),
            client_id=CLIENT,
            original_filename="claims2.edi",
            archive_root=root,
        )
    with session_scope() as session:
        ids = list(
            session.scalars(
                select(Claim.claim_id)
                .where(Claim.client_id == CLIENT)
                .order_by(Claim.submitter_claim_id)
            ).all()
        )
        for claim_id in ids:
            for target, facts in PATH_TO_READY:
                transition(
                    session,
                    claim_id=claim_id,
                    client_id=CLIENT,
                    target=target,
                    actor_type=ActorType.SYSTEM,
                    actor_id="system:test",
                    facts=facts,
                    next_action_due=date(2026, 6, 2),
                )
    return ids[0], ids[1]


def items_of(kind: WorkItemKind) -> list[WorkItem]:
    with session_scope() as session:
        return list(session.scalars(select(WorkItem).where(WorkItem.kind == str(kind))).all())


def test_a_reused_key_for_another_claim_is_a_conflict(
    two_ready_claims: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Returning claim A's row to claim B would say B was submitted."""
    from rcm.platform.errors import IdempotencyConflictError

    first, second = two_ready_claims
    enqueue(first, key="shared")
    with pytest.raises(IdempotencyConflictError):
        enqueue(second, key="shared")
    assert state_of(second) is ClaimState.READY_TO_SUBMIT


@pytest.mark.invariant
def test_a_lost_response_goes_to_a_person_and_is_never_resent(ready_claim: uuid.UUID) -> None:
    primary = MockAdapter("primary", loses_response=True)
    secondary = MockAdapter("secondary")
    r = router(primary, secondary)
    enqueue(ready_claim)

    report = dispatch(r)
    assert report.needs_review == 1
    (row,) = rows()
    assert row.status == str(OutboxStatus.NEEDS_REVIEW)
    assert len(items_of(WorkItemKind.EXTERNAL_SYSTEM_FAILURE)) == 1

    primary.loses_response = False
    dispatch(r, NOW + timedelta(hours=2))
    assert primary.transmissions == 1  # never sent again
    assert secondary.attempts == 0


def test_a_rejected_file_goes_to_a_person_and_the_batch_continues(
    two_ready_claims: tuple[uuid.UUID, uuid.UUID],
) -> None:
    first, second = two_ready_claims
    primary = MockAdapter("primary", rejecting=True)
    r = router(primary, MockAdapter("secondary"))
    enqueue(first, key="k-first")
    with session_scope() as session:
        outbox.dispatch(session, router=r, client_id=CLIENT, now=NOW, limit=1)
    primary.rejecting = False
    enqueue(second, key="k-second")
    dispatch(r)

    statuses = sorted(row.status for row in rows())
    assert statuses == ["REJECTED", "SENT"]
    (rejection,) = items_of(WorkItemKind.SUBMISSION_REJECTION)
    assert rejection.claim_id == first


def test_an_unexpected_adapter_failure_does_not_lose_the_batch(
    two_ready_claims: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """An adapter crash on one row is that row's problem: the rows already
    sent stay sent, and the crash goes to a person."""
    first, second = two_ready_claims
    primary = MockAdapter("primary")
    r = router(primary, MockAdapter("secondary"))
    enqueue(first, key="k-first")
    enqueue(second, key="k-second")

    class CrashOnSecond:
        name = "primary"

        def __init__(self) -> None:
            self.calls = 0

        def submit_837(self, payload: bytes, *, idempotency_key: str):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("connection reset")
            return primary.submit_837(payload, idempotency_key=idempotency_key)

        def fetch_responses(self) -> list[bytes]:
            return []

        def health_check(self) -> bool:
            return True

    r = ClearinghouseRouter(
        primary=CrashOnSecond(),
        secondary=MockAdapter("secondary"),
        failure_threshold=5,
        open_seconds=60,
    )
    report = dispatch(r)
    assert (report.sent, report.needs_review) == (1, 1)
    assert sorted(row.status for row in rows()) == ["NEEDS_REVIEW", "SENT"]


def test_retries_stop_after_the_maximum(
    ready_claim: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row cannot retry forever unnoticed."""
    from rcm.platform.config import get_settings

    monkeypatch.setenv("RCM_OUTBOX_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    try:
        down = router(MockAdapter("p", failing=True), MockAdapter("s", failing=True))
        enqueue(ready_claim)
        dispatch(down)
        dispatch(down, NOW + timedelta(hours=1))
    finally:
        monkeypatch.delenv("RCM_OUTBOX_MAX_ATTEMPTS")
        get_settings.cache_clear()
    (row,) = rows()
    assert row.status == str(OutboxStatus.NEEDS_REVIEW)
    assert row.attempts == 2


def test_rows_not_tried_in_an_outage_keep_their_attempt_count(
    two_ready_claims: tuple[uuid.UUID, uuid.UUID],
) -> None:
    first, second = two_ready_claims
    enqueue(first, key="k-first")
    enqueue(second, key="k-second")
    report = dispatch(router(MockAdapter("p", failing=True), MockAdapter("s", failing=True)))
    assert report.outage is True
    assert sorted(row.attempts for row in rows()) == [0, 1]
