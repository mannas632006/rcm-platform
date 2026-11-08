"""One claim, for S-03 Claim Detail (M5.5).

A member id, diagnoses and dates of service are PHI, so the read is audited
(PD-2). Client scope is applied in `rcm.work.claim_detail` (PD-20).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from rcm.api.dependencies import AuditedSession, CurrentPrincipal, require_permission
from rcm.api.schemas import (
    AUTH_ERRORS,
    CLAIM_ERRORS,
    CLAIM_SEARCH_ERRORS,
    ClaimDetailOut,
    ClaimSearchOut,
)
from rcm.platform.auth import Permission
from rcm.work import claim_detail, claim_search

router = APIRouter(prefix="/api/v1", tags=["claims"], responses=AUTH_ERRORS)


@router.get(
    "/claims",
    operation_id="searchClaims",
    summary="Find claims by claim number or payer",
    responses=CLAIM_SEARCH_ERRORS,
    dependencies=[require_permission(Permission.CLAIM_READ)],
)
def search_claims(
    q: Annotated[str, Query(min_length=2, max_length=80)],
    principal: CurrentPrincipal,
    session: AuditedSession,
    limit: Annotated[int, Query(ge=1, le=claim_search.MAX_RESULTS)] = claim_search.MAX_RESULTS,
) -> ClaimSearchOut:
    """The top bar's search (S-01): a claim number's start, a payer id, or
    part of a payer's name, inside your clients (PD-20). Dates of service
    are listed, so the read is audited (PD-2)."""
    found = claim_search.search_claims(session, principal=principal, q=q, limit=limit)
    return ClaimSearchOut.model_validate(found, from_attributes=True)


@router.get(
    "/claims/{claim_id}",
    operation_id="getClaim",
    summary="One claim, everything needed to decide on it",
    responses=CLAIM_ERRORS,
    dependencies=[require_permission(Permission.CLAIM_READ)],
)
def get_claim(
    claim_id: uuid.UUID, principal: CurrentPrincipal, session: AuditedSession
) -> ClaimDetailOut:
    """S-03: header and filing countdown (UR-07), service lines, each 835
    payment with its adjustments group and reason together (UR-08, PD-3),
    the resubmission chain (PD-7), the state history, and every automated
    decision with its version and inputs (UR-06)."""
    detail = claim_detail.claim_detail(
        session, principal=principal, claim_id=claim_id, today=datetime.now(UTC).date()
    )
    return ClaimDetailOut.model_validate(detail, from_attributes=True)
