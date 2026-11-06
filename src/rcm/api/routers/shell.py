"""What every screen's shell reads: who is signed in, the queues they may
see, and the filing-deadline alert (M5.1, SRS 10.4.0, UR-26).

The counts, charges and the caller's own identity carry no PHI and read an
ordinary session. A queue's items carry claim identifiers and dates of
service, so that route reads an audited one (PD-2). Client scope is applied
in `rcm.work.views` for all of them (PD-20).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from rcm.api.dependencies import AuditedSession, CurrentPrincipal, DbSession, require_permission
from rcm.api.schemas import (
    AUTH_ERRORS,
    QUEUE_ERRORS,
    DeadlineRiskOut,
    Me,
    QueueItemOut,
    QueueItemsPage,
    QueueSummaryOut,
    WorkQueues,
)
from rcm.platform.auth import Permission
from rcm.work import views

router = APIRouter(prefix="/api/v1", tags=["shell"], responses=AUTH_ERRORS)


@router.get("/me", operation_id="getMe", summary="Who is signed in")
def get_me(principal: CurrentPrincipal) -> Me:
    """The signed-in operator's roles, permissions and client scope."""
    return Me(
        subject=principal.subject,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
        client_ids=sorted(principal.client_ids),
    )


@router.get("/work-queues", operation_id="listWorkQueues", summary="Queues visible to me")
def list_work_queues(principal: CurrentPrincipal, session: DbSession) -> WorkQueues:
    """Each queue the operator's role may work, with its open work, inside
    the operator's client scope (SRS 7.2, PD-20)."""
    summaries = views.queue_summaries(session, principal=principal, now=datetime.now(UTC))
    return WorkQueues(
        queues=[
            QueueSummaryOut(
                key=s.key,
                label=s.label,
                open=s.open,
                unassigned=s.unassigned,
                mine=s.mine,
                pinned=s.pinned,
                overdue=s.overdue,
            )
            for s in summaries
        ]
    )


@router.get(
    "/work-queues/{queue_key}/items",
    operation_id="listQueueItems",
    summary="A queue's items, best first",
    responses=QUEUE_ERRORS,
)
def list_queue_items(
    queue_key: str,
    principal: CurrentPrincipal,
    session: AuditedSession,
    cursor: Annotated[str | None, Query(description="From the previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=views.MAX_PAGE)] = 200,
    assigned: Annotated[views.Assignment, Query()] = views.Assignment.ANY,
    payer_id: Annotated[str | None, Query(max_length=80)] = None,
) -> QueueItemsPage:
    """S-02: the queue in priority order (UR-02), deadline-pinned items first
    and never filtered out (UR-03). Cursor-paginated (FR-API-02).

    Rows carry claim identifiers and dates of service, so the read is
    audited (PD-2).
    """
    page = views.queue_items(
        session,
        principal=principal,
        key=queue_key,
        now=datetime.now(UTC),
        cursor=cursor,
        limit=limit,
        assignment=assigned,
        payer_id=payer_id,
    )
    return QueueItemsPage(
        items=[QueueItemOut.model_validate(item, from_attributes=True) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/alerts/deadline-risk",
    operation_id="getDeadlineRisk",
    summary="Claims at filing-deadline risk",
    dependencies=[require_permission(Permission.CLAIM_READ)],
)
def get_deadline_risk(principal: CurrentPrincipal, session: DbSession) -> DeadlineRiskOut:
    """UR-26's alert: claims whose filing deadline has pinned their work, and
    their billed charges. Claim money, so it needs `claim:read`."""
    risk = views.deadline_risk(session, principal=principal)
    return DeadlineRiskOut(claims=risk.claims, charges=risk.charges)
