"""INSIGHT and ADMIN reads: dashboard, rules, payer registry (M5.9-M5.11).

Read-only. The dashboard is a client's numbers, summed over the caller's
client scope in `rcm.work.insight` (PD-20); it carries counts and rates, no
PHI, so it reads an ordinary session. Rules and the payer registry are
platform reference data behind `rule:read`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from rcm.api.dependencies import CurrentPrincipal, DbSession, require_permission
from rcm.api.schemas import (
    AUTH_ERRORS,
    DashboardOut,
    DashboardSegmentsOut,
    PayerChoiceOut,
    PayerRegistryOut,
    RegistryEntryOut,
    RuleOut,
    RulesOut,
    StateCount,
)
from rcm.platform.auth import Permission
from rcm.platform.config import get_settings
from rcm.work import insight

router = APIRouter(prefix="/api/v1", tags=["insight"], responses=AUTH_ERRORS)


@router.get(
    "/dashboard",
    operation_id="getDashboard",
    summary="KPIs for my clients",
    dependencies=[require_permission(Permission.REPORT_READ)],
)
def get_dashboard(
    principal: CurrentPrincipal,
    session: DbSession,
    client_id: Annotated[
        uuid.UUID | None, Query(description="One of my clients; omit for all of them.")
    ] = None,
    payer_id: Annotated[
        str | None, Query(min_length=1, max_length=80, description="One payer, by payer_id.")
    ] = None,
) -> DashboardOut:
    """Denial rate, first-pass clean rate, claims by state and queue health,
    for all my clients and payers or segmented to one of each (UR-19). A
    client outside my scope is refused like any other (PD-20)."""
    d = insight.dashboard(
        session,
        principal=principal,
        now=datetime.now(UTC),
        client_id=client_id,
        payer_id=payer_id,
    )
    choices = insight.segments(session, principal=principal)
    return DashboardOut(
        adjudicated_claims=d.adjudicated_claims,
        denied_claims=d.denied_claims,
        denial_rate=d.denial_rate,
        first_pass_claims=d.first_pass_claims,
        first_pass_clean=d.first_pass_clean,
        first_pass_clean_rate=d.first_pass_clean_rate,
        claims_by_state=[StateCount(state=s, count=c) for s, c in d.claims_by_state.items()],
        open_work=d.open_work,
        pinned_work=d.pinned_work,
        overdue_work=d.overdue_work,
        steps_awaiting_review=d.steps_awaiting_review,
        client_id=client_id,
        payer_id=payer_id,
        segments=DashboardSegmentsOut(
            client_ids=choices.client_ids,
            payers=[PayerChoiceOut(payer_id=p.payer_id, name=p.name) for p in choices.payers],
        ),
    )


@router.get(
    "/rules",
    operation_id="listRules",
    summary="Rules, latest version each",
    dependencies=[require_permission(Permission.RULE_READ)],
)
def list_rules(principal: CurrentPrincipal, session: DbSession) -> RulesOut:
    """Each rule's latest version, status and latest backtest (M4.4-M4.6),
    with where Promote would take it and why it cannot yet (UR-30)."""
    return RulesOut(
        rules=[
            RuleOut.model_validate(row, from_attributes=True)
            for row in insight.rules(session, principal=principal)
        ],
        fp_rate_cap=get_settings().blocking_rule_max_false_positive_rate,
    )


@router.get(
    "/payer-registry",
    operation_id="listPayerRegistry",
    summary="Payer registry, with staleness",
    dependencies=[require_permission(Permission.RULE_READ)],
)
def list_payer_registry(session: DbSession) -> PayerRegistryOut:
    """Current registry entries, each flagged when past [TBD-42] (M4.10)."""
    return PayerRegistryOut(
        entries=[
            RegistryEntryOut.model_validate(row, from_attributes=True)
            for row in insight.payer_registry(session, today=datetime.now(UTC).date())
        ],
        staleness_days=get_settings().payer_registry_staleness_days,
    )
