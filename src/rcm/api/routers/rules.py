"""S-06 Promote: a rule version one step along its lifecycle (M5.9, UR-30).

The step itself is `rcm.decision.rules.lifecycle.change_status`, the only
way a rule's status changes; this route adds who asked and refuses a step
that is not the version's next promotion, so a screen showing an old status
cannot move a rule twice. Rules are platform reference data, not a client's,
and carry no PHI. Needs an Idempotency-Key; a retry promotes once (PD-14).
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from rcm.api.dependencies import CurrentPrincipal, DbSession, require_permission
from rcm.api.schemas import AUTH_ERRORS, RULE_PROMOTION_ERRORS, RulePromoteIn, RulePromotionOut
from rcm.decision.rules import lifecycle
from rcm.decision.rules.definition import RuleStatus
from rcm.decision.rules.models import RuleVersion
from rcm.platform.auth import Permission
from rcm.platform.errors import ResourceNotFoundError, StateTransitionError

router = APIRouter(prefix="/api/v1", tags=["rules"], responses=AUTH_ERRORS)


@router.post(
    "/rules/{rule_id}/activate",
    operation_id="promoteRule",
    summary="Promote a rule version one step",
    responses=RULE_PROMOTION_ERRORS,
    dependencies=[require_permission(Permission.RULE_PROMOTE)],
)
def promote_rule(
    rule_id: str, body: RulePromoteIn, principal: CurrentPrincipal, session: DbSession
) -> RulePromotionOut:
    """FR-RULES-06: draft -> backtest -> shadow -> advisory -> blocking, no
    shortcuts. Into SHADOW needs a recorded backtest; into BLOCKING needs a
    blocking severity and every client's false-positive rate within the cap
    (FR-RULES-03). A refusal says which (UR-30)."""
    row = session.scalar(
        select(RuleVersion)
        .where(RuleVersion.rule_id == rule_id, RuleVersion.version == body.version)
        # Held until commit, so the status checked here is the one stepped from.
        .with_for_update()
    )
    if row is None:
        raise ResourceNotFoundError(resource="rule_version")
    source = RuleStatus(row.status)
    to = RuleStatus(body.to_status)
    if lifecycle.PROMOTION.get(source) is not to:
        raise StateTransitionError(
            source=str(source), target=str(to), reason="not this version's next promotion"
        )
    change = lifecycle.change_status(
        session,
        rule_id=rule_id,
        version=body.version,
        to=to,
        changed_by=principal.subject,
        reason=body.reason,
    )
    return RulePromotionOut(
        rule_id=change.rule_id,
        version=change.version,
        from_status=str(change.from_status),
        to_status=str(change.to_status),
        fp_rate=change.fp_rate,
    )
