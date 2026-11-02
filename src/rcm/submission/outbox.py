"""Enqueue and dispatch claim submissions (M3.7, IR-06, IR-07, IR-09).

    IR-06 (MUST): Submission MUST use the transactional outbox pattern.
    Writing "submitted" to the database and transmitting the file must not
    be able to diverge.

`enqueue` moves the claim `ReadyToSubmit -> Submitted` and writes the outbox
row in the caller's transaction: both or neither. `Submitted` therefore
means "committed to be sent", which is what SRS 6.3's sequence needs -- the
claim is only *truly* submitted on a 277CA acceptance anyway (BR-M8-02).

`dispatch` sends what is due, one row at a time, and every outcome lands
somewhere a person can see:

- sent: `SENT`, with the receipt;
- rejected by the clearinghouse: `REJECTED`, and a SUBMISSION_REJECTION
  item for that claim;
- outcome unknown: `NEEDS_REVIEW` -- never resent automatically, because the
  file may already be at the payer -- and an item asking a person to check;
- no clearinghouse available: still `PENDING`, retried later, and an outage
  item saying how many claims are waiting; after [outbox_max_attempts]
  failed deliveries a row goes to `NEEDS_REVIEW` instead of retrying forever.

**Delivery is at least once.** Each sent row is flushed as it goes, but the
caller commits. A crash after a send and before the commit leaves the row
`PENDING`, and it is sent again with the same idempotency key -- which a
real clearinghouse adapter must honour (the mock does). Workers should
dispatch in small batches and commit each.

Both functions check the CLAIM_SUBMISSION suspension first (BR-EXC-03).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rcm.domain.claim import Claim
from rcm.domain.claim_lifecycle import transition
from rcm.domain.claim_state import ClaimState
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.config import get_settings
from rcm.platform.errors import (
    ExternalSystemOutcomeUnknownError,
    ExternalSystemRejectedError,
    ExternalSystemUnavailableError,
    IdempotencyConflictError,
    ResourceNotFoundError,
    ValidationFailedError,
)
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

    A retry with the same key and the same request returns the existing row
    and changes nothing (IR-07, EC-41). The same key with a different claim
    or file is a conflict, refused by name -- returning the other row would
    tell the caller a claim was submitted when it was not.
    """
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    if not payload:
        raise ValidationFailedError(field="payload", reason="nothing to submit")
    require_automation_allowed(session, client_id=client_id, path=AutomationPath.CLAIM_SUBMISSION)
    digest = hashlib.sha256(payload).hexdigest()

    # Lock the claim first, so a concurrent retry of this enqueue waits here
    # and then finds the row this one wrote.
    if (
        session.scalar(
            select(Claim.claim_id)
            .where(Claim.claim_id == claim_id, Claim.client_id == client_id)
            .with_for_update()
        )
        is None
    ):
        raise ResourceNotFoundError(entity="claim", entity_id=str(claim_id))

    existing = _by_key(session, client_id=client_id, idempotency_key=idempotency_key)
    if existing is not None:
        if existing.claim_id != claim_id or existing.payload_sha256 != digest:
            raise IdempotencyConflictError(resource="submission_outbox")
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
        payload_sha256=digest,
        status=str(OutboxStatus.PENDING),
        next_attempt_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as err:
        # Another claim took this key between the check and the insert.
        raise IdempotencyConflictError(resource="submission_outbox") from err

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


def _by_key(
    session: Session, *, client_id: uuid.UUID, idempotency_key: str
) -> SubmissionOutbox | None:
    return session.scalar(
        select(SubmissionOutbox).where(
            SubmissionOutbox.client_id == client_id,
            SubmissionOutbox.idempotency_key == idempotency_key,
        )
    )


@dataclass(frozen=True, slots=True)
class DispatchReport:
    sent: int = 0
    rejected: int = 0
    needs_review: int = 0
    deferred: int = 0
    #: True when no clearinghouse was available -- distinguishes "everything
    #: deferred" from "nothing was due".
    outage: bool = False


def dispatch(
    session: Session,
    *,
    router: ClearinghouseRouter,
    client_id: uuid.UUID,
    now: datetime,
    limit: int = 25,
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

    sent = rejected = review = deferred = 0
    outage = False
    for index, row in enumerate(due):
        try:
            receipt = router.submit_837(row.payload, idempotency_key=row.idempotency_key, now=now)
        except ExternalSystemRejectedError as err:
            row.attempts += 1
            row.status = str(OutboxStatus.REJECTED)
            row.last_error = err.code
            _raise_for_row(
                session,
                row=row,
                kind=WorkItemKind.SUBMISSION_REJECTION,
                problem="The clearinghouse received this claim's file and refused it",
                now=now,
            )
            rejected += 1
        except ExternalSystemOutcomeUnknownError as err:
            row.attempts += 1
            row.status = str(OutboxStatus.NEEDS_REVIEW)
            row.last_error = err.code
            _raise_for_row(
                session,
                row=row,
                kind=WorkItemKind.EXTERNAL_SYSTEM_FAILURE,
                problem="Clearinghouse response lost: confirm whether this claim was "
                "received before sending it again",
                now=now,
            )
            review += 1
        except ExternalSystemUnavailableError as err:
            outage = True
            # Only a row a clearinghouse was actually called for counts a try;
            # with every circuit open nothing was. The rest of the batch was
            # not tried either. All wait for the breakers to close.
            tried = err.context["attempted"] is True
            if tried:
                row.attempts += 1
            row.last_error = err.code
            if tried and row.attempts >= settings.outbox_max_attempts:
                row.status = str(OutboxStatus.NEEDS_REVIEW)
                _raise_for_row(
                    session,
                    row=row,
                    kind=WorkItemKind.EXTERNAL_SYSTEM_FAILURE,
                    problem="Claim could not be delivered to any clearinghouse after "
                    "repeated attempts",
                    now=now,
                )
                review += 1
            for waiting in due[index:]:
                if waiting.status == str(OutboxStatus.PENDING):
                    waiting.next_attempt_at = now + timedelta(seconds=settings.outbox_retry_seconds)
                    deferred += 1
            _raise_outage(session, client_id=client_id, row=row, now=now)
            break
        else:
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

    return DispatchReport(
        sent=sent, rejected=rejected, needs_review=review, deferred=deferred, outage=outage
    )


def _raise_for_row(
    session: Session, *, row: SubmissionOutbox, kind: WorkItemKind, problem: str, now: datetime
) -> None:
    raise_exception(
        session,
        client_id=row.client_id,
        kind=kind,
        problem=problem,
        record=RecordRef("submission_outbox", row.outbox_id),
        source_key=f"outbox:{row.outbox_id}:{kind}",
        claim_id=row.claim_id,
        now=now,
    )


def _raise_outage(
    session: Session, *, client_id: uuid.UUID, row: SubmissionOutbox, now: datetime
) -> None:
    """One item per day of outage, saying how many claims are waiting."""
    waiting = session.scalar(
        select(func.count()).where(
            SubmissionOutbox.client_id == client_id,
            SubmissionOutbox.status == str(OutboxStatus.PENDING),
        )
    )
    raise_exception(
        session,
        client_id=client_id,
        kind=WorkItemKind.EXTERNAL_SYSTEM_FAILURE,
        problem="No clearinghouse is accepting claim submissions",
        record=RecordRef("submission_outbox", row.outbox_id),
        source_key=f"clearinghouse_unavailable:{now.date().isoformat()}",
        detail=f"{waiting} claim submissions are waiting and will be retried automatically.",
        now=now,
    )
    log.error("clearinghouse.unavailable", waiting=waiting)
