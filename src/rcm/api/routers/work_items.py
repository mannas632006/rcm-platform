"""Taking, giving back and resolving a queue item (M5.4, SRS 10.9).

The queue keys `a` and `r` call these. Scope, role and the one-holder lock
are enforced in `rcm.work.holding`; every route needs an Idempotency-Key,
so a retried keystroke acts once (PD-14).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter

from rcm.api.dependencies import CurrentPrincipal, DbSession, require_permission
from rcm.api.schemas import (
    AUTH_ERRORS,
    WORK_ITEM_ERRORS,
    WorkItemHoldingOut,
    WorkItemResolutionIn,
)
from rcm.platform.auth import Permission
from rcm.work import holding
from rcm.work.state import WorkItemOutcome

router = APIRouter(prefix="/api/v1", tags=["work-items"], responses=AUTH_ERRORS)


@router.post(
    "/work-items/{work_item_id}/take",
    operation_id="takeWorkItem",
    summary="Assign an item to me",
    responses=WORK_ITEM_ERRORS,
    dependencies=[require_permission(Permission.CLAIM_READ)],
)
def take_work_item(
    work_item_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> WorkItemHoldingOut:
    """FR-WORK-01: an exclusive hold, refused by name if someone else has
    it. Taking an item already mine renews the lease (FR-WORK-02)."""
    held = holding.take(
        session, principal=principal, work_item_id=work_item_id, now=datetime.now(UTC)
    )
    return WorkItemHoldingOut.model_validate(held, from_attributes=True)


@router.post(
    "/work-items/{work_item_id}/release",
    operation_id="releaseWorkItem",
    summary="Give an item back to its queue",
    responses=WORK_ITEM_ERRORS,
    dependencies=[require_permission(Permission.CLAIM_READ)],
)
def release_work_item(
    work_item_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> WorkItemHoldingOut:
    """Only the holder may give an item back."""
    held = holding.give_back(
        session, principal=principal, work_item_id=work_item_id, now=datetime.now(UTC)
    )
    return WorkItemHoldingOut.model_validate(held, from_attributes=True)


@router.post(
    "/work-items/{work_item_id}/resolution",
    operation_id="resolveWorkItem",
    summary="Resolve an item that needs nothing more",
    responses=WORK_ITEM_ERRORS,
    dependencies=[require_permission(Permission.CLAIM_READ)],
)
def resolve_work_item(
    work_item_id: uuid.UUID,
    body: WorkItemResolutionIn,
    principal: CurrentPrincipal,
    session: DbSession,
) -> WorkItemHoldingOut:
    """FR-WORK-04: no action needed, or a duplicate, with the reason. A
    denial is refused here: it is decided in the Denial Workbench."""
    held = holding.resolve(
        session,
        principal=principal,
        work_item_id=work_item_id,
        outcome=WorkItemOutcome(body.outcome),
        note=body.note,
        now=datetime.now(UTC),
    )
    return WorkItemHoldingOut.model_validate(held, from_attributes=True)
