"""Response schemas for the operator UI's API (M5.1, SRS 7.2).

The frontend's types are generated from the OpenAPI schema these produce
(FR-API-05), so a field here is a field the UI is held to. Money is a decimal
string, never a float: the UI formats it with `MoneyDisplay` (UR-21) and a
float would have lost cents before it got there.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rcm.platform.auth import Permission, Role


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class Problem(_Out):
    """RFC 7807 Problem Details (FR-API-04): every error the API returns.

    Declared on each route so the generated client types the error body too.
    """

    type: str
    title: str
    status: int
    detail: str
    code: str = Field(description="Machine-readable error code, stable across releases.")
    instance: str | None = None
    request_id: str | None = Field(default=None, description="Correlates to logs (FR-API-03).")


#: The errors every authenticated route can return.
AUTH_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": Problem, "description": "No valid bearer token."},
    403: {"model": Problem, "description": "The token lacks the permission this route needs."},
}


class Me(_Out):
    """Who is signed in, and what they may do. No PHI: a staff identity."""

    subject: str
    roles: list[Role]
    permissions: list[Permission]
    client_ids: list[uuid.UUID]


class QueueSummaryOut(_Out):
    key: str = Field(description="Stable queue identifier, used in URLs.")
    label: str
    open: int
    unassigned: int
    mine: int = Field(description="Open items held by the signed-in operator.")
    pinned: int = Field(description="Deadline-pinned items (PD-15), always shown first.")
    overdue: int = Field(description="Open items past their SLA.")


class WorkQueues(_Out):
    queues: list[QueueSummaryOut]


class DeadlineRiskOut(_Out):
    """UR-26: the alert bar that cannot be dismissed while claims are at risk."""

    claims: int
    charges: Decimal = Field(
        description="Billed charges of the claims at risk, as an exact decimal string to the cent."
    )


class QueueItemOut(_Out):
    """One row of a queue (S-02). Charges are billed charges, to the cent."""

    work_item_id: uuid.UUID
    kind: str
    title: str
    state: str
    priority_score: int
    deadline_pinned: bool = Field(description="PD-15: always first, never filtered out (UR-03).")
    days_to_deadline: int | None
    assigned_to: str | None
    sla_due_at: datetime
    overdue: bool
    bulk_handling: bool
    created_at: datetime
    claim_id: uuid.UUID | None
    submitter_claim_id: str | None
    payer_id: str | None
    payer_name: str | None
    date_of_service: date | None
    charges: Decimal | None
    recovery_probability: float | None = Field(
        default=None,
        description="ML-3's P(recovery). Null until the model exists (M6): the UI "
        "shows it as unavailable, never as zero (SRS 10.8 PARTIAL).",
    )


class QueueItemsPage(_Out):
    items: list[QueueItemOut]
    next_cursor: str | None = Field(description="Pass back as `cursor` for the next page.")


#: A queue's item list can also refuse a queue or a cursor.
QUEUE_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "No such queue, or not one this role may see."},
    422: {"model": Problem, "description": "A cursor, limit or filter that is not valid."},
}


# ---------------------------------------------------------------------------
# INSIGHT and ADMIN (M5.9-M5.11, pulled forward)
# ---------------------------------------------------------------------------


class StateCount(_Out):
    state: str
    count: int


class DashboardOut(_Out):
    """KPIs for the signed-in operator's clients. Counts and rates only."""

    adjudicated_claims: int
    denied_claims: int
    denial_rate: Decimal | None = Field(
        description="Denied over adjudicated claims, per claim (BR-DATA-03), as a percentage."
    )
    first_pass_claims: int
    first_pass_clean: int
    first_pass_clean_rate: Decimal | None = Field(
        description="First submissions neither rejected nor denied, as a percentage."
    )
    claims_by_state: list[StateCount]
    open_work: int
    pinned_work: int
    overdue_work: int
    steps_awaiting_review: int = Field(
        description="Automatic claim steps waiting for the submitter's approval (ADR-0006)."
    )


class RuleOut(_Out):
    rule_id: str
    version: int
    layer: str
    severity: str
    status: str
    created_at: datetime
    backtest_flagged: int | None
    backtest_fp_rate: Decimal | None = Field(
        description="False-positive rate, 0-1, of the backtest promotion judges by: each "
        "client's latest that saw a claim, the worst of them (M4.6). Null when none has run "
        "or one flagged nothing."
    )


class RulesOut(_Out):
    rules: list[RuleOut]


class RegistryEntryOut(_Out):
    payer_id: str
    payer_name: str
    plan_type: str
    cpt: str
    version: int
    prior_auth_required: bool | None
    last_verified_date: date
    stale: bool = Field(description="Past [TBD-42]: cannot drive a BLOCKING decision (BR-REG-02).")


class PayerRegistryOut(_Out):
    entries: list[RegistryEntryOut]
    staleness_days: int


# ---------------------------------------------------------------------------
# S-03 Claim Detail (M5.5)
# ---------------------------------------------------------------------------

#: A claim outside the caller's scope reads exactly like one never issued.
CLAIM_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "No such claim, or not one in your clients."},
}


class FilingOut(_Out):
    status: str = Field(
        description="RUNNING (days_left set), FILED (the payer took it), UNKNOWN "
        "(no term or no date of service -- never a guessed deadline), or NOT_APPLICABLE "
        "(never filed and no longer will be: cancelled, auth denied, replaced, written off)."
    )
    days_left: int | None = Field(description="Negative once the deadline has passed.")


class ClaimLineOut(_Out):
    line_number: int
    procedure_code: str
    modifiers: list[str]
    units: Decimal
    charge: Decimal
    service_date: date | None
    diagnosis_pointers: list[int]


class AdjustmentOut(_Out):
    """One CAS adjustment (PD-6). The group code is never absent (PD-3)."""

    group_code: str
    reason_code: str
    amount: Decimal
    claim_level: bool


class PaidLineOut(_Out):
    line_number: int
    procedure_code: str
    modifiers: list[str]
    charge: Decimal
    allowed: Decimal | None
    paid: Decimal
    remark_codes: list[str]
    adjustments: list[AdjustmentOut]


class PaymentOut(_Out):
    """One 835 claim payment, as the payer sent it."""

    claim_payment_id: uuid.UUID
    submitter_claim_id: str = Field(description="The submission this payment answers (PD-7).")
    attempt: int
    payer_control_number: str
    status_code: str
    payment_date: date | None
    total_charge: Decimal
    total_paid: Decimal
    patient_responsibility: Decimal
    is_reversal: bool
    lines: list[PaidLineOut]
    claim_adjustments: list[AdjustmentOut]


class ChainLinkOut(_Out):
    claim_id: uuid.UUID
    submitter_claim_id: str
    attempt: int
    state: str


class TimelineEventOut(_Out):
    occurred_at: datetime
    source_state: str
    target_state: str
    reason: str
    note: str
    actor_type: str
    actor_id: str
    review_status: str


class DecisionOut(_Out):
    """UR-06: what was decided, by which logic version, on what inputs, when."""

    decision_id: uuid.UUID
    decided_at: datetime
    entity_type: str
    entity_id: uuid.UUID
    decision_type: str
    decided_by: str
    logic_version: str
    inputs: dict[str, Any]
    output: dict[str, Any]
    confidence: float | None


class ClaimDetailOut(_Out):
    claim_id: uuid.UUID
    submitter_claim_id: str
    state: str
    payer_id: str
    payer_name: str | None
    date_of_service: date | None
    submitted_on: date | None
    next_action_due: date | None
    total_charge: Decimal
    balance: Decimal
    member_id_masked: str | None = Field(
        description="Last four characters only (S-6); null when the claim carries none."
    )
    place_of_service: str
    diagnoses: list[str]
    filing: FilingOut
    lines: list[ClaimLineOut]
    payments: list[PaymentOut]
    chain: list[ChainLinkOut] = Field(description="Every submission of this claim (PD-7).")
    timeline: list[TimelineEventOut]
    decisions: list[DecisionOut]


# ---------------------------------------------------------------------------
# S-04 Denial Workbench (M5.7)
# ---------------------------------------------------------------------------

DENIAL_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "No such denial, or not one in your clients."},
}


class DenialClassificationOut(_Out):
    """As the classifier recorded it (BR-TAX-01), never re-derived."""

    category: str
    preventability: str
    group_code: str
    reason_code: str
    taxonomy_version: str


class DenialGuidanceOut(_Out):
    """What usually resolves this category (DOC-02 8.1). The same for every
    claim in it: documentation, not a decision about this claim."""

    category: str
    text: str
    version: str


class SimilarDenialsOut(_Out):
    """UR-11: other claims' denials with the same payer, procedure, group and
    reason in the window, and how the resolved ones ended."""

    window_days: int
    denials: int
    outcomes: dict[str, int]


class DeniedLineOut(_Out):
    line_number: int
    procedure_code: str
    modifiers: list[str]
    charge: Decimal
    paid: Decimal
    adjustments: list[AdjustmentOut]
    classification: DenialClassificationOut | None = Field(
        description="Null for a line the payer did not deny."
    )
    guidance: DenialGuidanceOut | None
    similar: SimilarDenialsOut | None


class DenialClaimOut(_Out):
    claim_id: uuid.UUID
    submitter_claim_id: str
    state: str
    payer_id: str
    payer_name: str | None
    date_of_service: date | None
    balance: Decimal


class DenialPaymentOut(_Out):
    claim_payment_id: uuid.UUID
    payer_control_number: str
    payment_date: date | None
    total_charge: Decimal
    total_paid: Decimal
    patient_responsibility: Decimal


class DenialWorkbenchOut(_Out):
    work_item_id: uuid.UUID
    state: str
    assigned_to: str | None
    sla_due_at: datetime
    outcome: str | None
    claim: DenialClaimOut | None = Field(
        description="Null when the 835 names a claim this client never submitted."
    )
    payment: DenialPaymentOut
    lines: list[DeniedLineOut]
