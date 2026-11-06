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
from rcm.api.schemas import AUTH_ERRORS, DENIAL_ERRORS, DenialWorkbenchOut
from rcm.platform.auth import Permission
from rcm.work import denial_workbench

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
