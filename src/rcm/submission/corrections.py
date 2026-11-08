"""Correct a denied claim and send the replacement (M5.7 slice 3, UR-10, ADR-0018).

**The operator sees the diff first.** `preview` shows every changed field,
before and after, and writes nothing. `resubmit` applies the same correction.

**The replacement is the original 837 with only the edits changed.** Claim
rows carry no patient demographics, so the corrected claim is rebuilt from
the archived file (PD-9) with the claim as stored laid over it, the operator's
edits applied, frequency 7 and REF*F8 naming the claim it corrects. It is
loaded through `load_claim_file`, the one place resubmissions chain (PD-7,
BR-DATA-03), so its own bytes are in RAW like every other 837.

**Charges are never edited here.** A different charge is a different bill,
and it belongs to charge entry, not to a denial.

**A replacement is scrubbed and sent in the same transaction.** Progression
reads any claim short of `Submitted` as "an 837 on file was sent"
(ADR-0006), so a replacement left at `Charged` would be called sent without
being sent. It is walked through `Scrubbing` and enqueued in the outbox
(IR-06) before the transaction ends, or the whole correction is refused: a
scrubber block, or a fix the scrubber would make itself, is named back to
the operator, who corrects again. The original waits in `Correcting` until
the payer takes the replacement, when progression moves it to `Replaced`.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.decision.rules.evaluate import Mode, Verdict
from rcm.decision.rules.scrub import scrub_claim
from rcm.domain.claim import MAX_MODIFIERS, Claim, ClaimLine
from rcm.domain.claim_lifecycle import transition
from rcm.domain.claim_state import ClaimState, Guard
from rcm.domain.remittance import RawFile
from rcm.ingestion.archive import read_archived
from rcm.ingestion.claim_loader import ClaimLoadOutcome, load_claim_file
from rcm.ingestion.x12 import (
    Claim837,
    ClaimServiceLine,
    ClaimSubmission,
    build_837,
    parse_837,
    read_interchange,
)
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.auth import Permission, Principal
from rcm.platform.config import get_settings
from rcm.platform.errors import ResourceNotFoundError, StateTransitionError, ValidationFailedError
from rcm.submission import outbox
from rcm.work.claim_detail import mask
from rcm.work.dispositions import appeal_in_draft, close_item, denial_item, waiting_write_off
from rcm.work.models import WorkItem
from rcm.work.state import WorkItemOutcome

__all__ = ["Change", "Correction", "LineCorrection", "Resubmitted", "preview", "resubmit"]

#: CLM01 is AN 1-38 in 005010X222A1.
_CLM01_MAX: Final = 38
#: 837P: twelve diagnosis codes on HI, four pointers on SV107.
_MAX_DIAGNOSES: Final = 12
_MAX_POINTERS: Final = 4
#: X12-safe identifiers: no element, component or segment separators.
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9-]{1,50}")
#: CPT (five digits, or four and a letter) and HCPCS Level II (a letter and four).
_PROCEDURE: Final = re.compile(r"[A-Z0-9]{5}")
_MODIFIER: Final = re.compile(r"[A-Z0-9]{2}")
#: ICD-10-CM without the dot, as HI carries it.
_DIAGNOSIS: Final = re.compile(r"[A-Z][0-9][0-9A-Z]{1,5}")
_MAX_UNITS: Final = Decimal("9999")


@dataclass(frozen=True, slots=True)
class LineCorrection:
    """What changes on one service line. None leaves a field as it is."""

    line_number: int
    procedure_code: str | None = None
    modifiers: tuple[str, ...] | None = None
    units: Decimal | None = None
    diagnosis_pointers: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class Correction:
    """What the operator changes. None leaves a field as it is."""

    member_id: str | None = None
    prior_auth_number: str | None = None
    diagnoses: tuple[str, ...] | None = None
    lines: tuple[LineCorrection, ...] = ()


@dataclass(frozen=True, slots=True)
class Change:
    """One field of the diff, as the operator reads it (UR-10)."""

    field: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class Resubmitted:
    claim_id: uuid.UUID
    submitter_claim_id: str
    changes: tuple[Change, ...]


def _joined(values: tuple[object, ...]) -> str:
    return ", ".join(str(v) for v in values)


def _quantity(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _on_file(
    session: Session, *, item: WorkItem, archive_root: Path | str
) -> tuple[Claim, ClaimSubmission, Claim837]:
    """The denied claim as this platform holds it: the archived 837 with the
    stored row's codes laid over it, since the scrubber may have corrected
    those (ADR-0011)."""
    claim = session.scalar(
        select(Claim).where(Claim.client_id == item.client_id, Claim.claim_id == item.claim_id)
    )
    if claim is None:
        raise ResourceNotFoundError(entity="claim", entity_id=str(item.claim_id))
    raw = session.scalar(
        select(RawFile).where(
            RawFile.client_id == claim.client_id, RawFile.raw_file_id == claim.raw_file_id
        )
    )
    if raw is None:
        raise ResourceNotFoundError(entity="raw_file", entity_id=str(claim.raw_file_id))
    interchange = read_interchange(read_archived(raw.storage_path, archive_root=archive_root))
    for transaction in interchange.transaction_sets("837"):
        submission = parse_837(transaction, interchange.separators)
        for parsed in submission.claims:
            if parsed.claim_id == claim.submitter_claim_id:
                return claim, submission, _overlay(session, claim=claim, parsed=parsed)
    # The file is the one this row was loaded from; not finding the claim in
    # it means RAW and CORE disagree, which nobody should paper over.
    raise ValidationFailedError(
        field="claim", reason="the archived 837 for this claim does not contain it"
    )


def _overlay(session: Session, *, claim: Claim, parsed: Claim837) -> Claim837:
    stored = {
        line.line_number: line
        for line in session.scalars(
            select(ClaimLine).where(
                ClaimLine.client_id == claim.client_id, ClaimLine.claim_id == claim.claim_id
            )
        )
    }
    lines = []
    for line in parsed.lines:
        row = stored.get(line.line_number)
        if row is None:
            lines.append(line)
            continue
        lines.append(
            replace(
                line,
                procedure_code=row.procedure_code,
                modifiers=tuple(
                    m for m in (row.modifier_1, row.modifier_2, row.modifier_3, row.modifier_4) if m
                ),
                units=row.units,
                diagnosis_pointers=tuple(row.diagnosis_pointers),
            )
        )
    return replace(
        parsed,
        subscriber=replace(parsed.subscriber, member_id=claim.subscriber_member_id),
        prior_auth_number=claim.prior_auth_number,
        diagnoses=tuple(claim.diagnoses),
        lines=tuple(lines),
    )


def _identifier(value: str, *, field: str, required: bool) -> str:
    value = value.strip()
    if not value and not required:
        return value
    if not _IDENTIFIER.fullmatch(value):
        raise ValidationFailedError(field=field, reason="letters, digits and hyphens, at most 50")
    return value


def _apply(current: Claim837, change: Correction) -> tuple[Claim837, list[Change]]:
    """The corrected claim and its diff, or a refusal naming the field."""
    diff: list[Change] = []
    corrected = current

    if change.member_id is not None:
        member = _identifier(change.member_id, field="member_id", required=True)
        if member != current.subscriber.member_id:
            # S-6: the diff is a response body, so the ID is masked like the
            # claim's; the full value is only in the 837s in RAW.
            diff.append(
                Change("member_id", mask(current.subscriber.member_id) or "", mask(member) or "")
            )
            corrected = replace(
                corrected, subscriber=replace(corrected.subscriber, member_id=member)
            )

    if change.prior_auth_number is not None:
        auth = _identifier(change.prior_auth_number, field="prior_auth_number", required=False)
        if auth != current.prior_auth_number:
            diff.append(Change("prior_auth_number", current.prior_auth_number, auth))
            corrected = replace(corrected, prior_auth_number=auth)

    if change.diagnoses is not None:
        codes = tuple(c.strip().upper().replace(".", "") for c in change.diagnoses)
        if not 1 <= len(codes) <= _MAX_DIAGNOSES:
            raise ValidationFailedError(field="diagnoses", reason="one to twelve codes")
        if bad := [c for c in codes if not _DIAGNOSIS.fullmatch(c)]:
            raise ValidationFailedError(
                field="diagnoses",
                reason=f"{len(bad)} not in ICD-10-CM form (a letter, a digit, then 1-5 more)",
            )
        if len(set(codes)) != len(codes):
            raise ValidationFailedError(field="diagnoses", reason="each code once")
        if codes != current.diagnoses:
            diff.append(Change("diagnoses", _joined(current.diagnoses), _joined(codes)))
            corrected = replace(corrected, diagnoses=codes)

    by_number = {line.line_number: line for line in corrected.lines}
    seen: set[int] = set()
    for edit in change.lines:
        line = by_number.get(edit.line_number)
        if line is None:
            raise ValidationFailedError(
                field="lines", reason=f"the claim has no line {edit.line_number}"
            )
        if edit.line_number in seen:
            raise ValidationFailedError(
                field="lines", reason=f"line {edit.line_number} is corrected twice"
            )
        seen.add(edit.line_number)
        by_number[edit.line_number] = _apply_line(line, edit, diff)

    corrected = replace(
        corrected, lines=tuple(by_number[line.line_number] for line in current.lines)
    )
    # Pointers index the diagnosis list, so both edits are checked together:
    # dropping a code can orphan a line nobody touched.
    for line in corrected.lines:
        if not line.diagnosis_pointers or any(
            not 1 <= p <= len(corrected.diagnoses) for p in line.diagnosis_pointers
        ):
            raise ValidationFailedError(
                field=f"line {line.line_number} diagnosis_pointers",
                reason=f"each must point at one of the claim's {len(corrected.diagnoses)} codes",
            )
    if not diff:
        raise ValidationFailedError(field="correction", reason="it changes nothing on the claim")
    return corrected, diff


def _apply_line(
    line: ClaimServiceLine, edit: LineCorrection, diff: list[Change]
) -> ClaimServiceLine:
    label = f"line {line.line_number}"
    if edit.procedure_code is not None:
        code = edit.procedure_code.strip().upper()
        if not _PROCEDURE.fullmatch(code):
            raise ValidationFailedError(
                field=f"{label} procedure_code", reason="a five-character CPT or HCPCS code"
            )
        if code != line.procedure_code:
            diff.append(Change(f"{label} procedure_code", line.procedure_code, code))
            line = replace(line, procedure_code=code)
    if edit.modifiers is not None:
        modifiers = tuple(m.strip().upper() for m in edit.modifiers if m.strip())
        if len(modifiers) > MAX_MODIFIERS:
            raise ValidationFailedError(field=f"{label} modifiers", reason="at most four")
        if any(not _MODIFIER.fullmatch(m) for m in modifiers):
            raise ValidationFailedError(
                field=f"{label} modifiers", reason="two letters or digits each"
            )
        if modifiers != line.modifiers:
            diff.append(Change(f"{label} modifiers", _joined(line.modifiers), _joined(modifiers)))
            line = replace(line, modifiers=modifiers)
    if edit.units is not None:
        if not Decimal("0") < edit.units <= _MAX_UNITS:
            raise ValidationFailedError(field=f"{label} units", reason="more than 0, at most 9999")
        if edit.units != line.units:
            diff.append(Change(f"{label} units", _quantity(line.units), _quantity(edit.units)))
            line = replace(line, units=edit.units)
    if edit.diagnosis_pointers is not None:
        pointers = tuple(edit.diagnosis_pointers)
        if not 1 <= len(pointers) <= _MAX_POINTERS or len(set(pointers)) != len(pointers):
            raise ValidationFailedError(
                field=f"{label} diagnosis_pointers", reason="one to four, each once"
            )
        if pointers != line.diagnosis_pointers:
            diff.append(
                Change(
                    f"{label} diagnosis_pointers",
                    _joined(line.diagnosis_pointers),
                    _joined(pointers),
                )
            )
            line = replace(line, diagnosis_pointers=pointers)
    return line


def _correctable(session: Session, *, principal: Principal, work_item_id: uuid.UUID) -> WorkItem:
    principal.require(Permission.DENIAL_DISPOSITION)
    item = denial_item(session, principal=principal, work_item_id=work_item_id)
    if item.claim_id is None:
        raise ValidationFailedError(
            field="work_item_id",
            reason="the denied payment names no claim on file; find the claim first",
        )
    # One decision at a time (ADR-0017).
    if waiting_write_off(session, item=item) is not None:
        raise StateTransitionError(
            source="write-off awaiting approval",
            target=str(ClaimState.CORRECTING),
            reason="a write-off of this denial is waiting for approval; decide that first",
        )
    if appeal_in_draft(session, item=item):
        raise StateTransitionError(
            source="appeal in draft",
            target=str(ClaimState.CORRECTING),
            reason="an appeal of this denial is being drafted; send it or decide against it first",
        )
    state = session.scalar(
        select(Claim.state).where(
            Claim.client_id == item.client_id, Claim.claim_id == item.claim_id
        )
    )
    if state != str(ClaimState.DENIED):
        raise StateTransitionError(
            source=str(state),
            target=str(ClaimState.CORRECTING),
            reason="only a denied claim is corrected from the Denial Workbench",
        )
    return item


def preview(
    session: Session,
    *,
    principal: Principal,
    work_item_id: uuid.UUID,
    correction: Correction,
    archive_root: Path | str,
) -> list[Change]:
    """UR-10: every field the correction would change. Writes nothing."""
    item = _correctable(session, principal=principal, work_item_id=work_item_id)
    _, _, current = _on_file(session, item=item, archive_root=archive_root)
    _, diff = _apply(current, correction)
    return diff


def _replacement_id(session: Session, claim: Claim) -> str:
    """CLM-1 -> CLM-1-R2 -> CLM-1-R3: CLM01 is unique per client, so each
    submission in the chain needs its own. Built from the chain's first
    CLM01, not by parsing this one, which may itself end in "-R2"."""
    root = session.scalar(
        select(Claim.submitter_claim_id).where(
            Claim.client_id == claim.client_id, Claim.claim_id == claim.root_claim_id
        )
    )
    suffix = f"-R{claim.submission_attempt + 1}"
    candidate = (root or claim.submitter_claim_id)[: _CLM01_MAX - len(suffix)] + suffix
    if session.scalar(
        select(Claim.claim_id).where(
            Claim.client_id == claim.client_id, Claim.submitter_claim_id == candidate
        )
    ):
        # Another claim already carries that CLM01; never chain onto it.
        raise StateTransitionError(
            source="replacement id taken",
            target=str(ClaimState.CORRECTING),
            reason=f"a claim numbered {candidate} already exists; resolve that first",
        )
    return candidate


#: Registered -> Charged for a replacement. Eligibility, the service and its
#: coding are the claim it corrects; they are carried, not observed.
def _carried_path(claim: Claim) -> tuple[tuple[ClaimState, dict[str, bool] | None], ...]:
    if claim.prior_auth_number:
        before_service: tuple[tuple[ClaimState, dict[str, bool] | None], ...] = (
            (ClaimState.AUTH_PENDING, {str(Guard.AUTH_REQUIRED): True}),
            (ClaimState.AUTH_APPROVED, None),
            (ClaimState.SERVICE_RENDERED, None),
        )
    else:
        before_service = ((ClaimState.SERVICE_RENDERED, {str(Guard.AUTH_NOT_REQUIRED): True}),)
    return (
        (ClaimState.ELIGIBILITY_VERIFIED, None),
        *before_service,
        (ClaimState.CODED, None),
        (ClaimState.CHARGED, None),
    )


def resubmit(
    session: Session,
    *,
    principal: Principal,
    work_item_id: uuid.UUID,
    correction: Correction,
    archive_root: Path | str,
    now: datetime,
    note: str = "",
) -> Resubmitted:
    """Correct the denied claim and send the replacement, all or nothing."""
    if now.tzinfo is None:
        raise ValidationFailedError(field="now", reason="time must be timezone-aware")
    item = _correctable(session, principal=principal, work_item_id=work_item_id)
    original, submission, current = _on_file(session, item=item, archive_root=archive_root)
    corrected, diff = _apply(current, correction)
    if session.scalar(
        select(Claim.claim_id).where(
            Claim.client_id == original.client_id, Claim.original_claim_id == original.claim_id
        )
    ):
        raise StateTransitionError(
            source="replaced",
            target=str(ClaimState.CORRECTING),
            reason="a corrected submission of this claim already exists",
        )

    replacement_id = _replacement_id(session, original)
    payload = build_837(
        replace(
            submission,
            # ISA13 is nine digits and must not repeat between interchanges.
            control_number=f"{secrets.randbelow(10**9):09d}",
            claims=(
                replace(
                    corrected,
                    claim_id=replacement_id,
                    claim_frequency_code="7",
                    # PD-7: the loader chains on this.
                    original_claim_id=original.submitter_claim_id,
                ),
            ),
            created_on=now.date(),
            created_time=now.strftime("%H%M"),
        )
    ).encode()
    loaded = load_claim_file(
        session,
        payload,
        client_id=original.client_id,
        original_filename=f"{replacement_id}.837",
        archive_root=archive_root,
        actor_id=principal.subject,
        received_at=now,
    )
    if loaded.outcome is not ClaimLoadOutcome.LOADED or loaded.resubmissions_chained != 1:
        # Our own file failing our own assertions is a bug, not a claim to
        # send: refuse, and roll back with it.
        raise ValidationFailedError(
            field="correction",
            reason=loaded.quarantine_reason or "the corrected claim did not load as a replacement",
        )
    claim = session.scalar(
        select(Claim).where(
            Claim.client_id == original.client_id, Claim.submitter_claim_id == replacement_id
        )
    )
    if claim is None:
        # Loaded above in this transaction; absent means the load lied.
        raise ResourceNotFoundError(entity="claim", entity_id=replacement_id)

    followup = now.date() + timedelta(days=get_settings().submission_ack_followup_days)
    transition(
        session,
        claim_id=original.claim_id,
        client_id=original.client_id,
        target=ClaimState.CORRECTING,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        # Until the payer takes the replacement, this is the live claim.
        next_action_due=followup,
        note=f"corrected as {replacement_id}",
    )
    for target, facts in _carried_path(claim):
        transition(
            session,
            claim_id=claim.claim_id,
            client_id=claim.client_id,
            target=target,
            actor_type=ActorType.USER,
            actor_id=principal.subject,
            facts=facts,
            next_action_due=now.date(),
            note=f"carried from {original.submitter_claim_id}, the claim it corrects",
        )
    transition(
        session,
        claim_id=claim.claim_id,
        client_id=claim.client_id,
        target=ClaimState.SCRUBBING,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        next_action_due=now.date(),
    )
    scrubbed = scrub_claim(
        session, claim_id=claim.claim_id, client_id=claim.client_id, today=now.date()
    )
    if scrubbed.evaluation.verdict is Verdict.BLOCK:
        messages = sorted(
            {f.message for f in scrubbed.evaluation.findings if f.mode is Mode.BLOCKING}
        )
        raise ValidationFailedError(
            field="correction",
            reason="the scrubber blocks the corrected claim: " + "; ".join(messages),
        )
    if scrubbed.corrections:
        # What is sent must be what the operator previewed, so the scrubber's
        # own fix goes back to them rather than onto the claim unseen.
        fixes = sorted({c.fix for c in scrubbed.corrections})
        raise ValidationFailedError(
            field="correction",
            reason="the scrubber would change the corrected claim (" + "; ".join(fixes) + "); "
            "include that in the correction",
        )
    transition(
        session,
        claim_id=claim.claim_id,
        client_id=claim.client_id,
        target=ClaimState.READY_TO_SUBMIT,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        next_action_due=now.date(),
    )
    outbox.enqueue(
        session,
        claim_id=claim.claim_id,
        client_id=claim.client_id,
        payload=payload,
        idempotency_key=f"correction:{item.work_item_id}",
        actor_id=principal.subject,
        now=now,
    )
    record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=principal.subject,
        action="denial.corrected_and_resubmitted",
        entity_type="claim",
        entity_id=original.claim_id,
        # Field names only; the values are in the two 837s in RAW.
        after_state={
            "replacement_claim_id": str(claim.claim_id),
            "work_item_id": str(item.work_item_id),
            "fields": [c.field for c in diff],
        },
    )
    close_item(
        session,
        item=item,
        operator=principal.subject,
        outcome=WorkItemOutcome.CORRECTED_AND_RESUBMITTED,
        note=note.strip(),
        now=now,
    )
    return Resubmitted(claim.claim_id, replacement_id, tuple(diff))
