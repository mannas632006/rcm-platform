"""Enqueue and dispatch claim submissions (M3.7, IR-06, IR-07, IR-09).

    IR-06 (MUST): Submission MUST use the transactional outbox pattern.
    Writing "submitted" to the database and transmitting the file must not
    be able to diverge.

`enqueue` moves the claim `ReadyToSubmit -> Submitted` and writes the outbox
row in the caller's transaction: both or neither. `Submitted` therefore
means "committed to be sent", which is what SRS 6.3's sequence needs -- the
claim is only *truly* submitted on a 277CA acceptance anyway (BR-M8-02).

`dispatch` sends what is due through the router. When no clearinghouse is
available the row stays `PENDING` with a later retry time, and one
EXTERNAL_SYSTEM_FAILURE exception tells a person -- nothing is dropped.

Both check the CLAIM_SUBMISSION suspension first (BR-EXC-03).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.domain.claim_lifecycle import transition
from rcm.domain.claim_state import ClaimState
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.config import get_settings
from rcm.platform.errors import ExternalSystemUnavailableError, ValidationFailedError
from rcm.platform.logging import get_logger
from rcm.submission.models import OutboxStatus, SubmissionOutbox
from rcm.submission.router import ClearinghouseRouter
from rcm.work.exceptions import (
    AutomationPath,
    RecordRef,
    raise_exception,
    require_automation_allowed,
)
from rcm.work.models import WorkItemKind

log = get_logger(__name__)


def enqueue(
    session: Session,
    *,
    claim_id: uuid.UUID,
    client_id: uuid.UUID,
    payload: bytes,
    idempotency_key: str,
    actor_id: str,
    now: datetime,
) -> SubmissionOutbox:
    """Commit a claim to be sent: state change and outbox row, together.

    A retry with the same key returns the existing row and changes nothing
    (IR-07, EC-41).
    """
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    if not payload:
        raise ValidationFailedError(field="payload", reason="nothing to submit")
    require_automation_allowed(session, client_id=client_id, path=AutomationPath.CLAIM_SUBMISSION)

    existing = session.scalar(
        select(SubmissionOutbox).where(
            SubmissionOutbox.client_id == client_id,
            SubmissionOutbox.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    settings = get_settings()
    transition(
        session,
        claim_id=claim_id,
        client_id=client_id,
        target=ClaimState.SUBMITTED,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        # BR-STATE-04: if no acknowledgement arrives by then, someone looks.
        next_action_due=now.date() + timedelta(days=settings.submission_ack_followup_days),
    )
    row = SubmissionOutbox(
        client_id=client_id,
        claim_id=claim_id,
        idempotency_key=idempotency_key,
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        status=str(OutboxStatus.PENDING),
        next_attempt_at=now,
    )
    session.add(row)
    session.flush()
    record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="claim.submission_enqueued",
        entity_type="claim",
        entity_id=claim_id,
        after_state={"outbox_id": str(row.outbox_id), "payload_sha256": row.payload_sha256},
    )
    session.flush()
    return row


@dataclass(frozen=True, slots=True)
class DispatchReport:
    sent: int
    deferred: int


def dispatch(
    session: Session,
    *,
    router: ClearinghouseRouter,
    client_id: uuid.UUID,
    now: datetime,
    limit: int = 100,
) -> DispatchReport:
    """Send what is due. Rows are locked as they are read (SKIP LOCKED), so
    two dispatchers never send the same row."""
    require_automation_allowed(session, client_id=client_id, path=AutomationPath.CLAIM_SUBMISSION)
    settings = get_settings()

    due = session.scalars(
        select(SubmissionOutbox)
        .where(
            SubmissionOutbox.client_id == client_id,
            SubmissionOutbox.status == str(OutboxStatus.PENDING),
            SubmissionOutbox.next_attempt_at <= now,
        )
        .order_by(SubmissionOutbox.next_attempt_at, SubmissionOutbox.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()

    sent = deferred = 0
    for index, row in enumerate(due):
        try:
            receipt = router.submit_837(row.payload, idempotency_key=row.idempotency_key, now=now)
        except ExternalSystemUnavailableError as err:
            # No route at all: defer this row and the rest of the batch --
            # the breakers are open, and trying each row would only repeat
            # the refusal. Nothing is lost; every row is still PENDING.
            for waiting in due[index:]:
                waiting.attempts += 1
                waiting.last_error = err.code
                waiting.next_attempt_at = now + timedelta(seconds=settings.outbox_retry_seconds)
                deferred += 1
            raise_exception(
                session,
                client_id=client_id,
                kind=WorkItemKind.EXTERNAL_SYSTEM_FAILURE,
                problem="No clearinghouse is accepting claim submissions",
                record=RecordRef("submission_outbox", row.outbox_id),
                source_key="clearinghouse_unavailable",
                now=now,
            )
            log.error("clearinghouse.unavailable", deferred=deferred)
            break

        row.status = str(OutboxStatus.SENT)
        row.sent_at = now
        row.sent_via = receipt.adapter
        row.receipt_id = receipt.receipt_id
        row.attempts += 1
        row.last_error = ""
        record_audit_event(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id="system:outbox_dispatcher",
            action="claim.submission_sent",
            entity_type="claim",
            entity_id=row.claim_id,
            after_state={"via": receipt.adapter, "receipt_id": receipt.receipt_id},
        )
        sent += 1

    session.flush()
    return DispatchReport(sent=sent, deferred=deferred)
