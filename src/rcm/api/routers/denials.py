"""One denial, for S-04 Denial Workbench (M5.7).

Denied lines carry procedure codes and dates of service for a claim, so the
read is audited (PD-2). Client scope is applied in
`rcm.work.denial_workbench` (PD-20).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter

from rcm.api.dependencies import AuditedSession, CurrentPrincipal, require_permission
from rcm.api.schemas import (
    AUTH_ERRORS,
    DENIAL_ERRORS,
    DISPOSITION_ERRORS,
    DenialWorkbenchOut,
    DispositionIn,
    WriteOffDecisionIn,
    WriteOffDecisionOut,
    WriteOffOut,
)
from rcm.platform.auth import Permission
from rcm.work import denial_workbench, dispositions
from rcm.work.models import WriteOffReason

router = APIRouter(prefix="/api/v1", tags=["denials"], responses=AUTH_ERRORS)


@router.get(
    "/denials/{work_item_id}",
    operation_id="getDenial",
    summary="One denial, with what is needed to decide on it",
    responses=DENIAL_ERRORS,
    dependencies=[require_permission(Permission.DENIAL_READ)],
)
def get_denial(
    work_item_id: uuid.UUID, principal: CurrentPrincipal, session: AuditedSession
) -> DenialWorkbenchOut:
    """S-04: each line the payer denied, group and reason together (PD-3),
    with its recorded category and preventability (BR-TAX-01), guidance for
    the category, and how the same denial went before (UR-11)."""
    found = denial_workbench.denial_workbench(
        session, principal=principal, work_item_id=work_item_id, now=datetime.now(UTC)
    )
    return DenialWorkbenchOut.model_validate(found, from_attributes=True)


@router.post(
    "/denials/{work_item_id}/disposition",
    operation_id="disposeDenial",
    summary="Decide what to do with a denial",
    responses=DISPOSITION_ERRORS,
    dependencies=[require_permission(Permission.DENIAL_DISPOSITION)],
)
def dispose_denial(
    work_item_id: uuid.UUID,
    body: DispositionIn,
    principal: CurrentPrincipal,
    session: AuditedSession,
) -> WriteOffOut:
    """S-04. Write off the denial's payer share (BR-M11-05): applied at once
    within the operator's limit, otherwise left for a named approver. Needs
    an Idempotency-Key; a retry writes off once (PD-14)."""
    result = dispositions.request_write_off(
        session,
        principal=principal,
        work_item_id=work_item_id,
        reason=WriteOffReason(body.reason),
        note=body.note,
        now=datetime.now(UTC),
    )
    return WriteOffOut.model_validate(result, from_attributes=True)


@router.post(
    "/write-offs/{write_off_id}/decision",
    operation_id="decideWriteOff",
    summary="Approve or reject a write-off that waits for approval",
    responses=DISPOSITION_ERRORS,
    dependencies=[require_permission(Permission.WRITE_OFF_APPROVE)],
)
def decide_write_off(
    write_off_id: uuid.UUID,
    body: WriteOffDecisionIn,
    principal: CurrentPrincipal,
    session: AuditedSession,
) -> WriteOffDecisionOut:
    """BR-M11-05: a named approver who is not the requester."""
    status = dispositions.decide_write_off(
        session,
        principal=principal,
        write_off_id=write_off_id,
        approve=body.approve,
        note=body.note,
        now=datetime.now(UTC),
    )
    return WriteOffDecisionOut(write_off_id=write_off_id, status=status.value)
