"""What every screen's shell reads: who is signed in, the queues they may
see, and the filing-deadline alert (M5.1, SRS 10.4.0, UR-26).

Counts and charges only -- no PHI -- so these read an ordinary session, not
an audited one. Client scope is applied in `rcm.work.views` (PD-20).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from rcm.api.dependencies import CurrentPrincipal, DbSession, require_permission
from rcm.api.schemas import AUTH_ERRORS, DeadlineRiskOut, Me, QueueSummaryOut, WorkQueues
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
