"""The exception manager: named, owned, SLA-bound (M3.5).

    BR-EXC-01 (MUST): Every exception MUST name the specific problem and the
    specific record. Generic failure messages are defects.
    BR-EXC-02 (MUST): Every exception MUST have an owner and an SLA.
    BR-EXC-03 (MUST): Compliance-category exceptions MUST suspend the
    affected automation path until reviewed.
    BR-EXC-04 (MUST): Exception volume by type MUST be monitored.
    BR-EXC-05 (MUST): No exception may be closed without a recorded
    resolution -- structural already, in the work item's outcome CHECK.

`raise_exception` is the one way to put a problem in front of a person.
The caller says *what kind* of problem, *what specifically* is wrong, and
*which record*; everything else -- owner, SLA, priority, suspension --
follows from DOC-02 section 10.1's routing table here, so no caller can
forget it.

**Records are named by surrogate id, never by a patient identifier.** The
title is shown on queue screens. CLM01 is usually a patient account number,
which is a HIPAA identifier, so the record arrives as a `RecordRef` (entity
type plus UUID) and the title is built from that -- a caller cannot put a
member id in a title by accident.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.config import get_settings
from rcm.platform.errors import ComplianceError, StateTransitionError, ValidationFailedError
from rcm.platform.logging import get_logger
from rcm.work import queue
from rcm.work.models import (
    AutomationPath,
    AutomationSuspension,
    ExceptionCategory,
    WorkItem,
    WorkItemKind,
)
from rcm.work.state import OPEN_STATES, WorkItemState

__all__ = [
    "AutomationPath",
    "ExceptionCategory",
    "RecordRef",
    "VolumeTrend",
    "exception_volume",
    "lift_suspension",
    "raise_exception",
    "require_automation_allowed",
]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RecordRef:
    """The specific record an exception is about, by surrogate key."""

    entity_type: str
    entity_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Route:
    """Where one kind of exception goes (DOC-02 section 10.1)."""

    category: ExceptionCategory
    owner_role: str
    #: Deadline risk "overrides all" (DOC-02 10.1, PD-15): pinned at 100.
    pinned: bool = False


#: DOC-02 section 10.1, as data. Every kind has a route; a kind added to the
#: enum without one fails `test_every_kind_has_a_route`.
ROUTES: Final[dict[WorkItemKind, Route]] = {
    WorkItemKind.DATA_QUALITY: Route(ExceptionCategory.DATA_QUALITY, "ops_correction"),
    WorkItemKind.PAYER_MATCH_REVIEW: Route(ExceptionCategory.DATA_QUALITY, "ops_correction"),
    WorkItemKind.RULE_CONFLICT: Route(ExceptionCategory.RULE_CONFLICT, "rules_review"),
    WorkItemKind.MODEL_LOW_CONFIDENCE: Route(
        ExceptionCategory.MODEL_LOW_CONFIDENCE, "domain_specialist"
    ),
    WorkItemKind.EXTERNAL_SYSTEM_FAILURE: Route(ExceptionCategory.EXTERNAL_SYSTEM, "technical"),
    WorkItemKind.COMPLIANCE_CONCERN: Route(ExceptionCategory.COMPLIANCE, "compliance_officer"),
    WorkItemKind.DEADLINE_RISK: Route(
        ExceptionCategory.DEADLINE_RISK, "priority_queue", pinned=True
    ),
    WorkItemKind.MISSING_FILING_DEADLINE: Route(
        ExceptionCategory.DEADLINE_RISK, "priority_queue", pinned=True
    ),
    WorkItemKind.SCRUB_EXCEPTION: Route(ExceptionCategory.OPERATIONAL, "billing_specialist"),
    WorkItemKind.SUBMISSION_REJECTION: Route(ExceptionCategory.OPERATIONAL, "billing_specialist"),
    WorkItemKind.DENIAL: Route(ExceptionCategory.OPERATIONAL, "denials_specialist"),
    WorkItemKind.PAYMENT_EXCEPTION: Route(ExceptionCategory.OPERATIONAL, "payment_poster"),
    WorkItemKind.ELIGIBILITY_PROBLEM: Route(
        ExceptionCategory.OPERATIONAL, "eligibility_specialist"
    ),
    WorkItemKind.AUTH_PROBLEM: Route(ExceptionCategory.OPERATIONAL, "auth_specialist"),
}

#: What the user is told when a payer's filing deadline is unknown. Owner
#: decision (2026-11-01): ask for it, and say plainly what happens if they
#: do not answer.
MISSING_DEADLINE_DETAIL: Final = (
    "Please enter this payer's timely-filing deadline (in days). Until you do, "
    "every claim for this payer is treated as urgent and kept at the top of the "
    "queue, because a missed deadline means the claim can never be paid."
)

#: BR-EXC-01. Problems that say something failed without saying what.
_GENERIC: Final = re.compile(
    r"^\s*(an?\s+)?(unknown\s+)?(error|failure|failed|exception|problem|issue|unknown)"
    r"(\s+(occurred|happened|raised))?\s*[.!]*\s*$"
    r"|^\s*something\s+went\s+wrong\s*[.!]*\s*$"
    r"|^\s*$",
    re.IGNORECASE,
)

#: A problem shorter than this cannot name what is wrong.
_MIN_PROBLEM_LENGTH: Final = 12
_TITLE_LIMIT: Final = 200


def _sla_due(category: ExceptionCategory, now: datetime) -> datetime:
    hours = get_settings().exception_sla_hours.get(str(category))
    if hours is None or hours <= 0:
        # PD-18: an exception with no SLA is BR-EXC-02 broken quietly.
        raise ValidationFailedError(
            field="exception_sla_hours",
            reason=f"BR-EXC-02: no positive SLA is configured for {category}",
        )
    return now + timedelta(hours=hours)


def _title(problem: str, record: RecordRef) -> str:
    if _GENERIC.match(problem) or len(problem.strip()) < _MIN_PROBLEM_LENGTH:
        raise ValidationFailedError(
            field="problem",
            reason="BR-EXC-01: name the specific problem; a generic message is a defect",
        )
    suffix = f" ({record.entity_type} {str(record.entity_id)[:8]})"
    return problem.strip()[: _TITLE_LIMIT - len(suffix)] + suffix


def raise_exception(
    session: Session,
    *,
    client_id: uuid.UUID,
    kind: WorkItemKind,
    problem: str,
    record: RecordRef,
    source_key: str,
    now: datetime,
    claim_id: uuid.UUID | None = None,
    detail: str = "",
    suspends: AutomationPath | None = None,
) -> WorkItem:
    """Put a specific problem in front of the right person, once.

    Idempotent on `(client_id, kind, source_key)` while an item for it is
    open: a loader that re-reads the same bad file gets the same item back.
    """
    route = ROUTES[kind]
    title = _title(problem, record)

    if (route.category is ExceptionCategory.COMPLIANCE) != (suspends is not None):
        raise ValidationFailedError(
            field="suspends",
            reason="BR-EXC-03: a compliance exception names the automation path it "
            "suspends, and only a compliance exception suspends one",
        )

    existing = session.scalar(
        select(WorkItem).where(
            WorkItem.client_id == client_id,
            WorkItem.kind == str(kind),
            WorkItem.source_key == source_key,
            WorkItem.state.in_([str(s) for s in OPEN_STATES]),
        )
    )
    if existing is not None:
        return existing

    if kind is WorkItemKind.MISSING_FILING_DEADLINE and not detail:
        detail = MISSING_DEADLINE_DETAIL

    item = queue.create(
        session,
        client_id=client_id,
        kind=kind,
        category=route.category,
        title=title,
        owner_role=route.owner_role,
        sla_due_at=_sla_due(route.category, now),
        now=now,
        claim_id=claim_id,
        detail=detail,
        priority_score=100 if route.pinned else 0,
        deadline_pinned=route.pinned,
        source_key=source_key,
    )

    if suspends is not None:
        session.add(
            AutomationSuspension(
                client_id=client_id,
                path=str(suspends),
                work_item_id=item.work_item_id,
                raised_at=now,
            )
        )
        record_audit_event(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id="system:exception_manager",
            action="automation.suspended",
            entity_type="work_item",
            entity_id=item.work_item_id,
            after_state={"path": str(suspends)},
        )
        session.flush()
        log.warning("automation.suspended", path=str(suspends))

    log.info("exception.raised", kind=str(kind), category=str(route.category))
    return item


def require_automation_allowed(
    session: Session, *, client_id: uuid.UUID, path: AutomationPath
) -> None:
    """Refuse to run a suspended automation path (BR-EXC-03, FR-BE-07).

    Every automation path calls this before acting. Failing closed is the
    point: a suspension nobody checks is a compliance concern logged and
    then ignored.
    """
    active = session.scalar(
        select(AutomationSuspension.work_item_id).where(
            AutomationSuspension.client_id == client_id,
            AutomationSuspension.path == str(path),
            AutomationSuspension.lifted_at.is_(None),
        )
    )
    if active is not None:
        raise ComplianceError(path=str(path), raised_by=str(active))


def lift_suspension(
    session: Session,
    *,
    client_id: uuid.UUID,
    work_item_id: uuid.UUID,
    operator: str,
    now: datetime,
) -> None:
    """Restore the path, once the compliance item has been reviewed.

    *"Until reviewed"* is enforced, not trusted: the item that raised the
    suspension must already be resolved, with its structured outcome.
    """
    item = session.scalar(
        select(WorkItem).where(
            WorkItem.work_item_id == work_item_id, WorkItem.client_id == client_id
        )
    )
    if item is None or WorkItemState(item.state) is not WorkItemState.RESOLVED:
        raise StateTransitionError(
            source=item.state if item else "missing",
            target="SuspensionLifted",
            reason="BR-EXC-03: resolve the compliance item before lifting its suspension",
        )

    suspensions = session.scalars(
        select(AutomationSuspension)
        .where(
            AutomationSuspension.client_id == client_id,
            AutomationSuspension.work_item_id == work_item_id,
            AutomationSuspension.lifted_at.is_(None),
        )
        .with_for_update()
    ).all()
    for suspension in suspensions:
        suspension.lifted_at = now
        suspension.lifted_by = operator
        record_audit_event(
            session,
            actor_type=ActorType.USER,
            actor_id=operator,
            action="automation.suspension_lifted",
            entity_type="work_item",
            entity_id=work_item_id,
            after_state={"path": suspension.path},
        )
    session.flush()


@dataclass(frozen=True, slots=True)
class VolumeTrend:
    """Exceptions of one kind in the latest window and the one before."""

    kind: str
    current: int
    previous: int
    rising: bool


def exception_volume(
    session: Session, *, client_id: uuid.UUID, now: datetime, window_days: int = 7
) -> list[VolumeTrend]:
    """BR-EXC-04: counts by kind, and whether each is rising.

    A rising trend in one kind usually means something upstream broke -- a
    payer changed a format, a feed started sending blanks -- and it shows
    here before any single operator notices more of the same item.
    """
    settings = get_settings()
    window = timedelta(days=window_days)
    current_start, previous_start = now - window, now - 2 * window

    rows = session.execute(
        select(
            WorkItem.kind,
            func.count().filter(WorkItem.created_at >= current_start).label("current"),
            func.count().filter(WorkItem.created_at < current_start).label("previous"),
        )
        .where(
            WorkItem.client_id == client_id,
            WorkItem.created_at >= previous_start,
            WorkItem.created_at < now,
        )
        .group_by(WorkItem.kind)
        .order_by(WorkItem.kind)
    ).all()

    return [
        VolumeTrend(
            kind=row.kind,
            current=row.current,
            previous=row.previous,
            rising=(
                row.current >= settings.exception_trend_min_count
                and row.current >= row.previous * settings.exception_trend_ratio
            ),
        )
        for row in rows
    ]
