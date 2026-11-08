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
    AppealOut,
    AppealRevisionIn,
    AppealSignIn,
    CorrectionChangeOut,
    CorrectionIn,
    CorrectionOut,
    CorrectionPreviewOut,
    DenialWorkbenchOut,
    DispositionIn,
    PatientShareIn,
    PatientShareOut,
    WriteOffDecisionIn,
    WriteOffDecisionOut,
    WriteOffOut,
)
from rcm.platform.auth import Permission
from rcm.platform.config import get_settings
from rcm.submission import corrections
from rcm.work import appeals, denial_workbench, dispositions
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


@router.post(
    "/denials/{work_item_id}/patient-responsibility",
    operation_id="billDenialToPatient",
    summary="Bill a denial's patient share to the patient",
    responses=DISPOSITION_ERRORS,
    dependencies=[require_permission(Permission.DENIAL_DISPOSITION)],
)
def bill_denial_to_patient(
    work_item_id: uuid.UUID,
    body: PatientShareIn,
    principal: CurrentPrincipal,
    session: AuditedSession,
) -> PatientShareOut:
    """ADR-0016: accept the payer's refusal and move the denied lines' PR
    share to the patient. A CO amount is never billed to the patient (PD-19).
    Needs an Idempotency-Key; a retry bills once (PD-14)."""
    result = dispositions.bill_patient(
        session,
        principal=principal,
        work_item_id=work_item_id,
        note=body.note,
        now=datetime.now(UTC),
    )
    return PatientShareOut(amount=result.amount)


@router.post(
    "/denials/{work_item_id}/appeal",
    operation_id="draftAppeal",
    summary="Open a denial's appeal: its draft, or a new one",
    responses=DISPOSITION_ERRORS,
    dependencies=[require_permission(Permission.DENIAL_DISPOSITION)],
)
def draft_appeal(
    work_item_id: uuid.UUID, principal: CurrentPrincipal, session: AuditedSession
) -> AppealOut:
    """ADR-0017: a template letter with prompts a person must replace."""
    view = appeals.draft_appeal(
        session, principal=principal, work_item_id=work_item_id, now=datetime.now(UTC)
    )
    return AppealOut.model_validate(view, from_attributes=True)


@router.post(
    "/appeals/{appeal_id}/versions",
    operation_id="reviseAppeal",
    summary="Save a change to an appeal letter as a new version",
    responses=DISPOSITION_ERRORS,
    dependencies=[require_permission(Permission.DENIAL_DISPOSITION)],
)
def revise_appeal(
    appeal_id: uuid.UUID,
    body: AppealRevisionIn,
    principal: CurrentPrincipal,
    session: AuditedSession,
) -> AppealOut:
    view = appeals.revise_appeal(
        session,
        principal=principal,
        appeal_id=appeal_id,
        letter=body.letter,
        now=datetime.now(UTC),
    )
    return AppealOut.model_validate(view, from_attributes=True)


@router.post(
    "/appeals/{appeal_id}/sign",
    operation_id="signAppeal",
    summary="Sign the current letter and send the appeal",
    responses=DISPOSITION_ERRORS,
    dependencies=[require_permission(Permission.APPEAL_SIGN)],
)
def sign_appeal(
    appeal_id: uuid.UUID,
    body: AppealSignIn,
    principal: CurrentPrincipal,
    session: AuditedSession,
) -> AppealOut:
    """UR-09: refused while any [prompt] is left or the version is not the
    current one. Needs an Idempotency-Key; a retry sends once (PD-14)."""
    view = appeals.sign_and_send(
        session,
        principal=principal,
        appeal_id=appeal_id,
        version=body.version,
        now=datetime.now(UTC),
    )
    return AppealOut.model_validate(view, from_attributes=True)


def _correction(body: CorrectionIn) -> corrections.Correction:
    return corrections.Correction(
        member_id=body.member_id,
        prior_auth_number=body.prior_auth_number,
        diagnoses=None if body.diagnoses is None else tuple(body.diagnoses),
        lines=tuple(
            corrections.LineCorrection(
                line_number=line.line_number,
                procedure_code=line.procedure_code,
                modifiers=None if line.modifiers is None else tuple(line.modifiers),
                units=line.units,
                diagnosis_pointers=(
                    None if line.diagnosis_pointers is None else tuple(line.diagnosis_pointers)
                ),
            )
            for line in body.lines
        ),
    )


def _changes(
    changes: list[corrections.Change] | tuple[corrections.Change, ...],
) -> list[CorrectionChangeOut]:
    return [CorrectionChangeOut(field=c.field, before=c.before, after=c.after) for c in changes]


@router.post(
    "/denials/{work_item_id}/correction/preview",
    operation_id="previewDenialCorrection",
    summary="Show every field a correction would change, changing nothing",
    responses=DISPOSITION_ERRORS,
    dependencies=[require_permission(Permission.DENIAL_DISPOSITION)],
)
def preview_correction(
    work_item_id: uuid.UUID,
    body: CorrectionIn,
    principal: CurrentPrincipal,
    session: AuditedSession,
) -> CorrectionPreviewOut:
    """UR-10: the field-level diff the operator reads before resubmitting."""
    changes = corrections.preview(
        session,
        principal=principal,
        work_item_id=work_item_id,
        correction=_correction(body),
        archive_root=get_settings().raw_archive_root,
    )
    return CorrectionPreviewOut(changes=_changes(changes))


@router.post(
    "/denials/{work_item_id}/correction",
    operation_id="resubmitDenialCorrection",
    summary="Correct a denied claim and send the replacement",
    responses=DISPOSITION_ERRORS,
    dependencies=[require_permission(Permission.DENIAL_DISPOSITION)],
)
def resubmit_correction(
    work_item_id: uuid.UUID,
    body: CorrectionIn,
    principal: CurrentPrincipal,
    session: AuditedSession,
) -> CorrectionOut:
    """ADR-0018: a frequency-7 replacement chained to the denied claim
    (PD-7), scrubbed and queued to send in one transaction. Needs an
    Idempotency-Key; a retry resubmits once (PD-14)."""
    done = corrections.resubmit(
        session,
        principal=principal,
        work_item_id=work_item_id,
        correction=_correction(body),
        archive_root=get_settings().raw_archive_root,
        now=datetime.now(UTC),
        note=body.note,
    )
    return CorrectionOut(
        claim_id=done.claim_id,
        submitter_claim_id=done.submitter_claim_id,
        changes=_changes(done.changes),
    )
