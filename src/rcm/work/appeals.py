"""Appealing a denial: draft, revise, sign and send (M5.7 slice 2, ADR-0017).

    UR-09 (MUST): appeal letters are always human-reviewed before
    submission. No "send" path bypasses review.

**The draft is a template, not a model.** It states the claim, the denied
lines with group and reason together (PD-3), and an evidence checklist for
the denial's category -- and leaves bracketed prompts, "[State why ...]", that
only a person can fill. Signing is refused while any prompt is left: an
unreviewed letter cannot be sent.

**Signing binds to the words read.** The signer names a version; it must be
the current one, and the SHA-256 of its text is recorded. A version someone
changed after the signer read it cannot be sent under their name.

**"Send" records the appeal as sent.** Delivering it to the payer (portal,
fax, mail, an X12 275 attachment) is a real connection, a human checkpoint
(CLAUDE.md 12); the claim moves to Appealing and is followed up after
`appeal_follow_up_days` (a labelled placeholder).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcm.decision.denials import DenialCategory
from rcm.domain.claim import Claim
from rcm.domain.claim_lifecycle import transition
from rcm.domain.claim_state import ClaimState
from rcm.domain.payer import Payer
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.auth import Permission, Principal, scoped_query
from rcm.platform.config import get_settings
from rcm.platform.errors import (
    ResourceNotFoundError,
    StateTransitionError,
    ValidationFailedError,
)
from rcm.work import denial_workbench
from rcm.work.dispositions import close_item, denial_item, waiting_write_off
from rcm.work.models import Appeal, AppealSignoff, AppealVersion, WorkItem
from rcm.work.state import WorkItemOutcome

#: A prompt left for a person: "[State why ...]". Signing refuses any bracket
#: at all, after NFKC folds full-width ones to ASCII: a prompt broken over
#: lines, or half deleted, is still an unreviewed prompt (UR-09).
PROMPT: Final = re.compile(r"[\[\]]")


def has_prompt(text: str) -> bool:
    return PROMPT.search(unicodedata.normalize("NFKC", text)) is not None


#: What the payer will want enclosed, by denial category (DOC-02 8.1).
EVIDENCE: Final[dict[DenialCategory, list[str]]] = {
    DenialCategory.REGISTRATION_ELIGIBILITY: [
        "Eligibility response for the date of service",
        "Copy of the member card",
    ],
    DenialCategory.AUTHORISATION: [
        "Authorisation number and approval letter",
        "Record of when authorisation was requested",
    ],
    DenialCategory.CODING: ["Clinical notes for the date of service", "Coding rationale"],
    DenialCategory.MEDICAL_NECESSITY: [
        "Clinical notes for the date of service",
        "Ordering provider's statement of medical necessity",
        "Relevant coverage policy (LCD/NCD or payer policy)",
    ],
    DenialCategory.TIMELY_FILING: [
        "Proof of timely filing (clearinghouse acceptance report or 277CA)"
    ],
    DenialCategory.DUPLICATE: ["Explanation of how this claim differs from the earlier one"],
    DenialCategory.COORDINATION_OF_BENEFITS: [
        "Primary payer's remittance",
        "Coordination of benefits information",
    ],
    DenialCategory.CONTRACTUAL_NON_COVERED: ["Contract terms showing the service is covered"],
    DenialCategory.PROVIDER_ENROLMENT: [
        "Proof of the provider's enrolment for the date of service"
    ],
    DenialCategory.PAYER_ERROR: ["The remittance showing the error", "Supporting records"],
}


@dataclass(frozen=True, slots=True)
class AppealView:
    appeal_id: uuid.UUID
    version: int
    letter: str
    evidence: list[str]
    #: DRAFT until signed and sent; SENT after.
    status: str
    written_by: str
    signed_by: str | None
    signed_at: datetime | None


def _view(session: Session, appeal: Appeal) -> AppealView:
    latest = session.scalar(
        select(AppealVersion)
        .where(AppealVersion.appeal_id == appeal.appeal_id)
        .order_by(AppealVersion.version.desc())
        .limit(1)
    )
    if latest is None:  # every appeal is created with its first version
        raise ResourceNotFoundError(entity="appeal_version", entity_id=str(appeal.appeal_id))
    signoff = session.get(AppealSignoff, appeal.appeal_id)
    return AppealView(
        appeal_id=appeal.appeal_id,
        version=latest.version,
        letter=latest.letter,
        evidence=list(latest.evidence),
        status="SENT" if signoff else "DRAFT",
        written_by=latest.written_by,
        signed_by=signoff.signed_by if signoff else None,
        signed_at=signoff.signed_at if signoff else None,
    )


def current_appeal(session: Session, *, item: WorkItem) -> AppealView | None:
    """The denial's appeal, newest first, if one was drafted."""
    appeal = session.scalar(
        select(Appeal)
        .where(Appeal.client_id == item.client_id, Appeal.work_item_id == item.work_item_id)
        .order_by(Appeal.created_at.desc())
        .limit(1)
    )
    return _view(session, appeal) if appeal else None


def _template(
    session: Session, *, principal: Principal, item: WorkItem, now: datetime
) -> tuple[str, list[str]]:
    found = denial_workbench.denial_workbench(
        session, principal=principal, work_item_id=item.work_item_id, now=now
    )
    claim = session.scalar(
        select(Claim).where(Claim.client_id == item.client_id, Claim.claim_id == item.claim_id)
    )
    if claim is None or found.claim is None:
        raise ValidationFailedError(
            field="work_item_id", reason="the denial names no claim on file"
        )
    payer = session.get(Payer, claim.payer_id)
    evidence: list[str] = []
    rows: list[str] = []
    for line in found.lines:
        if line.classification is None:
            continue
        codes = ", ".join(f"{a.group_code}-{a.reason_code}" for a in line.adjustments)
        rows.append(
            f"- Line {line.line_number}, procedure {line.procedure_code}, billed "
            f"{line.charge}: denied {codes}"
        )
        for need in EVIDENCE.get(DenialCategory(line.classification.category), []):
            if need not in evidence:
                evidence.append(need)
    service = found.claim.date_of_service.isoformat() if found.claim.date_of_service else "[date]"
    letter = "\n".join(
        [
            f"To: {payer.name if payer else claim.payer_id}, Appeals",
            "Re: Request for reconsideration",
            "",
            f"Claim number: {claim.submitter_claim_id}",
            f"Payer claim control number: {found.payment.payer_control_number}",
            f"Member ID: {claim.subscriber_member_id}",
            f"Date of service: {service}",
            "",
            "We ask you to reconsider the denial of:",
            *rows,
            "",
            "[State why the denial should be reversed, citing the record.]",
            "",
            "Enclosed:",
            *(f"- {need}" for need in evidence),
            "",
            "[Name, title and contact details of the person signing]",
        ]
    )
    return letter, evidence


def _require_denied(session: Session, *, item: WorkItem) -> None:
    state = session.scalar(
        select(Claim.state).where(
            Claim.client_id == item.client_id, Claim.claim_id == item.claim_id
        )
    )
    if state != str(ClaimState.DENIED):
        raise StateTransitionError(
            source=str(state),
            target=str(ClaimState.APPEALING),
            reason="only a denied claim is appealed from the Denial Workbench",
        )


def draft_appeal(
    session: Session, *, principal: Principal, work_item_id: uuid.UUID, now: datetime
) -> AppealView:
    """Open the denial's appeal: the existing draft, or a new one."""
    principal.require(Permission.DENIAL_DISPOSITION)
    item = denial_item(session, principal=principal, work_item_id=work_item_id)
    if item.claim_id is None:
        raise ValidationFailedError(
            field="work_item_id",
            reason="the denied payment names no claim on file; find the claim first",
        )
    if waiting_write_off(session, item=item) is not None:
        raise StateTransitionError(
            source="write-off awaiting approval",
            target=str(ClaimState.APPEALING),
            reason="a write-off of this denial is waiting for approval; decide that first",
        )
    _require_denied(session, item=item)
    existing = current_appeal(session, item=item)
    if existing is not None and existing.status == "DRAFT":
        return existing

    letter, evidence = _template(session, principal=principal, item=item, now=now)
    appeal = Appeal(
        client_id=item.client_id,
        work_item_id=item.work_item_id,
        claim_id=item.claim_id,
        created_by=principal.subject,
        created_at=now,
    )
    session.add(appeal)
    session.flush()
    session.add(
        AppealVersion(
            appeal_id=appeal.appeal_id,
            version=1,
            client_id=item.client_id,
            letter=letter,
            evidence=evidence,
            written_by=principal.subject,
            written_at=now,
        )
    )
    session.flush()
    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        action="appeal.drafted",
        entity_type="appeal",
        entity_id=appeal.appeal_id,
        after_state={"work_item_id": str(item.work_item_id), "version": 1},
    )
    return _view(session, appeal)


def _appeal(session: Session, *, principal: Principal, appeal_id: uuid.UUID) -> Appeal:
    appeal = session.scalar(
        scoped_query(select(Appeal).where(Appeal.appeal_id == appeal_id), Appeal, principal)
    )
    if appeal is None:
        raise ResourceNotFoundError(entity="appeal", entity_id=str(appeal_id))
    return appeal


def revise_appeal(
    session: Session, *, principal: Principal, appeal_id: uuid.UUID, letter: str, now: datetime
) -> AppealView:
    """Save a person's change as a new version; the old one stays."""
    principal.require(Permission.DENIAL_DISPOSITION)
    appeal = _appeal(session, principal=principal, appeal_id=appeal_id)
    # Serialise on the denial's item: the appeal tables are insert-only.
    denial_item(session, principal=principal, work_item_id=appeal.work_item_id)
    if session.get(AppealSignoff, appeal_id) is not None:
        raise StateTransitionError(
            source="sent", target="revised", reason="this appeal has been sent; it cannot change"
        )
    if not letter.strip():
        raise ValidationFailedError(field="letter", reason="an appeal letter cannot be empty")
    current = _view(session, appeal)
    version = current.version + 1
    session.add(
        AppealVersion(
            appeal_id=appeal_id,
            version=version,
            client_id=appeal.client_id,
            letter=letter.strip(),
            evidence=current.evidence,
            written_by=principal.subject,
            written_at=now,
        )
    )
    session.flush()
    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        action="appeal.revised",
        entity_type="appeal",
        entity_id=appeal_id,
        after_state={"version": version},
    )
    return _view(session, appeal)


def sign_and_send(
    session: Session, *, principal: Principal, appeal_id: uuid.UUID, version: int, now: datetime
) -> AppealView:
    """UR-09: a person signs the exact current text, and it is sent."""
    principal.require(Permission.APPEAL_SIGN)
    appeal = _appeal(session, principal=principal, appeal_id=appeal_id)
    if session.get(AppealSignoff, appeal_id) is not None:
        raise StateTransitionError(
            source="sent", target="sent", reason="this appeal has already been sent"
        )
    item = denial_item(session, principal=principal, work_item_id=appeal.work_item_id)
    current = session.scalar(
        select(func.max(AppealVersion.version)).where(AppealVersion.appeal_id == appeal_id)
    )
    if version != current:
        raise StateTransitionError(
            source=f"version {version}",
            target="signed",
            reason=f"version {current} is newer; read and sign the current text",
        )
    text = session.scalar(
        select(AppealVersion.letter).where(
            AppealVersion.appeal_id == appeal_id, AppealVersion.version == version
        )
    )
    if text is None:
        raise ResourceNotFoundError(entity="appeal_version", entity_id=f"{appeal_id}/{version}")
    if has_prompt(text):
        raise ValidationFailedError(
            field="letter",
            reason="replace every [bracketed] prompt before signing; the letter is not reviewed",
        )
    if waiting_write_off(session, item=item) is not None:
        # Sending closes the denial; a waiting write-off would be stranded.
        raise StateTransitionError(
            source="write-off awaiting approval",
            target=str(ClaimState.APPEALING),
            reason="a write-off of this denial is waiting for approval; decide that first",
        )
    _require_denied(session, item=item)

    digest = hashlib.sha256(text.encode()).hexdigest()
    session.add(
        AppealSignoff(
            appeal_id=appeal_id,
            client_id=appeal.client_id,
            version=version,
            letter_sha256=digest,
            signed_by=principal.subject,
            signed_at=now,
        )
    )
    session.flush()
    days = get_settings().appeal_follow_up_days
    transition(
        session,
        claim_id=appeal.claim_id,
        client_id=appeal.client_id,
        target=ClaimState.APPEALING,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        next_action_due=now.date() + timedelta(days=days),
        note=f"appeal sent, version {version}",
    )
    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        action="appeal.signed_and_sent",
        entity_type="appeal",
        entity_id=appeal_id,
        after_state={"version": version, "letter_sha256": digest},
    )
    close_item(
        session,
        item=item,
        operator=principal.subject,
        outcome=WorkItemOutcome.APPEALED,
        note=f"appeal version {version}",
        now=now,
    )
    return _view(session, appeal)
