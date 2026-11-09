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
from typing import Any, Literal

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
    recovery_unavailable: str | None = Field(
        default=None,
        description="UR-34 PARTIAL: why this page carries no P(recovery), or null when the "
        "model answered. The queue is ordered on rules alone either way.",
    )


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
    next_status: str | None = Field(
        description="Where Promote would take this version (S-06); null once BLOCKING or "
        "DEPRECATED."
    )
    promotion_blocked_by: str | None = Field(
        description="UR-30: why promotion would be refused now, judged on every client's "
        "backtests as the step itself is. Null when it would be taken."
    )


class RulesOut(_Out):
    rules: list[RuleOut]
    fp_rate_cap: Decimal = Field(
        description="FR-RULES-03 / TBD-45: a rule above this false-positive rate may not be "
        "BLOCKING."
    )


RULE_PROMOTION_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "No such rule version."},
    409: {
        "model": Problem,
        "description": "The step is not the version's next promotion, or the lifecycle refuses "
        "it: no backtest, an unmeasured or too-high false-positive rate, a severity that never "
        "blocks, or another version already live.",
    },
}


class RulePromotionOut(_Out):
    rule_id: str
    version: int
    from_status: str
    to_status: str
    fp_rate: Decimal | None = Field(
        description="The false-positive rate the step rested on, for SHADOW and BLOCKING."
    )


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


class OpenProblemOut(_Out):
    work_item_id: uuid.UUID
    kind: str
    title: str
    detail: str
    owner_role: str
    assigned_to: str | None
    sla_due_at: datetime


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
    open_problems: list[OpenProblemOut] = Field(
        description="Open work items on any submission of this claim, soonest SLA first "
        "(M5.8): each names its problem (BR-EXC-01)."
    )


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
    status: str = Field(
        description="DENIED (classified), UNCLASSIFIED (paid nothing for a refusal the "
        "taxonomy does not hold yet -- a denial awaiting a category), or NOT_DENIED."
    )
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


class WriteOffSummaryOut(_Out):
    write_off_id: uuid.UUID
    status: Literal["APPLIED", "PENDING_APPROVAL", "REJECTED"]
    amount: Decimal
    reason: str
    note: str
    requested_by: str
    requested_at: datetime
    decided_by: str | None


class AppealOut(_Out):
    """UR-09: a DRAFT until a person signs the current text; SENT after."""

    appeal_id: uuid.UUID
    version: int
    letter: str
    evidence: list[str]
    status: Literal["DRAFT", "SENT"]
    written_by: str
    signed_by: str | None
    signed_at: datetime | None


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
    claim_adjustments: list[AdjustmentOut] = Field(description="CAS at claim level (PD-6).")
    write_offs: list[WriteOffSummaryOut] = Field(
        description="Every write-off asked of this denial, newest first."
    )
    appeal: AppealOut | None = Field(description="The denial's appeal, if one was drafted.")


class _In(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RulePromoteIn(_In):
    """S-06 Promote: one step along FR-RULES-06's path, named so a stale
    screen cannot promote twice."""

    version: int = Field(ge=1)
    to_status: Literal["BACKTESTING", "SHADOW", "ADVISORY", "BLOCKING"]
    reason: str = Field(min_length=1, max_length=2000)


class DispositionIn(_In):
    """S-04: what the operator decided. Write-off only in this slice."""

    action: Literal["WRITE_OFF"]
    reason: Literal[
        "APPEAL_NOT_VIABLE", "TIMELY_FILING_MISSED", "CONTRACTUAL", "SMALL_BALANCE", "OTHER"
    ] = Field(description='Structured (FR-WORK-04); "OTHER" needs a note.')
    note: str = Field(default="", max_length=2000)


class WriteOffOut(_Out):
    write_off_id: uuid.UUID
    status: Literal["APPLIED", "PENDING_APPROVAL", "REJECTED"]
    amount: Decimal = Field(description="The denied lines' payer share; never the PR amount.")
    threshold: Decimal = Field(
        description="[TBD-48] when asked. Above it, a specialist's write-off waits for a "
        "named approver (BR-M11-05)."
    )


class WriteOffDecisionIn(_In):
    approve: bool
    note: str = Field(default="", max_length=2000)


class WriteOffDecisionOut(_Out):
    write_off_id: uuid.UUID
    status: Literal["APPLIED", "REJECTED"]


DISPOSITION_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem, "description": "No such denial, or not one in your clients."},
    409: {
        "model": Problem,
        "description": "The claim is not denied, a write-off already waits, or another "
        "operator holds the item.",
    },
    422: {"model": Problem, "description": "A reason or note that is not valid."},
}


class PatientShareIn(_In):
    note: str = Field(default="", max_length=2000)


class PatientShareOut(_Out):
    amount: Decimal = Field(
        description="The denied lines' PR share billed to the patient. Never a CO amount (PD-19)."
    )


class AppealRevisionIn(_In):
    letter: str = Field(min_length=1, max_length=20000)


class AppealSignIn(_In):
    version: int = Field(
        ge=1, description="The version the signer read; it must be the current one."
    )


class LineCorrectionIn(_In):
    """One service line's edits. An omitted field is left as it is."""

    line_number: int = Field(ge=1)
    procedure_code: str | None = Field(default=None, max_length=5)
    modifiers: list[str] | None = Field(default=None, max_length=4)
    units: Decimal | None = Field(default=None, gt=0, max_digits=7, decimal_places=3)
    diagnosis_pointers: list[int] | None = Field(default=None, max_length=4)


class CorrectionIn(_In):
    """UR-10: what the operator changes. Charges are never edited here; an
    omitted field is left as it is."""

    member_id: str | None = Field(default=None, max_length=50)
    prior_auth_number: str | None = Field(
        default=None, max_length=50, description="An empty string removes it."
    )
    diagnoses: list[str] | None = Field(default=None, max_length=12)
    lines: list[LineCorrectionIn] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=2000)


class CorrectionChangeOut(_Out):
    field: str
    before: str
    after: str


class CorrectionPreviewOut(_Out):
    changes: list[CorrectionChangeOut] = Field(
        description="Every field the correction changes. Nothing is written."
    )


class CorrectionOut(_Out):
    claim_id: uuid.UUID = Field(description="The replacement claim, chained to the original.")
    submitter_claim_id: str
    changes: list[CorrectionChangeOut]


# --- M5.4: taking and resolving queue items ----------------------------------

WORK_ITEM_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {
        "model": Problem,
        "description": "No such item, not one in your clients, or not one your role works.",
    },
    409: {"model": Problem, "description": "Another operator holds the item, or it is closed."},
    422: {"model": Problem, "description": "An outcome or note that is not valid here."},
}


class WorkItemHoldingOut(_Out):
    """Who holds an item now, and until when (FR-WORK-01, FR-WORK-02)."""

    work_item_id: uuid.UUID
    state: str
    assigned_to: str | None
    lock_expires_at: datetime | None
    outcome: str | None


class WorkItemResolutionIn(_In):
    """FR-WORK-04: a structured outcome, and why. A denial is resolved by its
    disposition in the Denial Workbench, never here."""

    outcome: Literal["NO_ACTION_NEEDED", "DUPLICATE"]
    note: str = Field(min_length=1, max_length=2000)


# --- GATE M5: claim search ----------------------------------------------------


class ClaimHitOut(_Out):
    claim_id: uuid.UUID
    submitter_claim_id: str
    submission_attempt: int
    state: str
    payer_id: str
    payer_name: str | None
    date_of_service: date | None
    total_charge: Decimal


class ClaimSearchOut(_Out):
    """Claims matching the search, newest service first; `truncated` when
    more matched than are listed."""

    hits: list[ClaimHitOut]
    truncated: bool


CLAIM_SEARCH_ERRORS: dict[int | str, dict[str, Any]] = {
    422: {"model": Problem, "description": "Search text shorter than 2 or longer than 80."},
}
