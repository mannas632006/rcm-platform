"""Load submitted 837P claims into CORE, chaining resubmissions (M1.9).

Same order as the remittance loader, for the same reasons -- archive,
recognise, parse, assert, load -- so the two differ only where the
transactions differ. What is specific to claims is the chaining.

**BR-DATA-03 / PD-7.** A resubmission is not a new claim. It arrives with its
own CLM01 and a `REF*F8` naming the claim it replaces, and the load attaches it
to that claim's chain rather than starting a new one. Getting this wrong is not
a visible failure: the rows are all there, every total is internally
consistent, and the only symptom is that the business appears to submit more
claims and be denied more often than it is.

**An unknown original is a refusal, not a guess.** If `REF*F8` names a claim
this platform has not loaded, the file is quarantined under `IR-05` rather than
loaded as an original claim. The alternative -- treat it as a new claim because
the parent is missing -- produces precisely the inflated volume BR-DATA-03
forbids, and does it silently. Files can legitimately arrive out of order, and
the recovery for that is to re-present the file once the original has landed:
the bytes are in RAW, reprocessing is idempotent, and the second attempt
succeeds. A quarantine that resolves itself on retry is a much better failure
than a wrong number nobody can see.

**Three assertions guard the load**, each named so an operator knows what
failed without opening the file:

    IR-05  a resubmission names a claim already loaded
    IR-06  the file contains at least one 837
    IR-07  CLM02 equals the sum of its service line charges
    IR-08  every service line carries a diagnosis pointer (BR-M6-01)

IR-07 is the claim-side counterpart of the 835's balance assertion. A claim
whose stated total disagrees with its own lines is not "mostly right"; it is a
file whose contents are not understood, and submitting or reporting the part
that happens to parse is how a ledger acquires numbers nobody can account for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.domain.claim import MAX_MODIFIERS, Claim, ClaimLine
from rcm.domain.payer import Payer
from rcm.domain.payer_registry import record_alias
from rcm.domain.remittance import RawFile, RawFileStatus
from rcm.ingestion.archive import ArchivedFile, archive
from rcm.ingestion.x12 import Claim837 as ParsedClaim
from rcm.ingestion.x12 import ClaimSubmission as ParsedSubmission
from rcm.ingestion.x12 import Limits, parse_837, read_interchange
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.errors import PlatformError, QuarantineError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

ASSERTION_PARSEABLE: Final = "IR-01: file parses as X12"
ASSERTION_KNOWN_ORIGINAL: Final = "IR-05: a resubmission names a claim already loaded"
ASSERTION_CONTAINS_CLAIM: Final = "IR-06: file contains at least one 837"
ASSERTION_CHARGES_RECONCILE: Final = "IR-07: CLM02 == sum(SV102)"
ASSERTION_DIAGNOSIS_POINTER: Final = "IR-08: every service line carries a diagnosis pointer"


class ClaimLoadOutcome(StrEnum):
    """What happened to one inbound 837."""

    LOADED = "LOADED"
    ALREADY_LOADED = "ALREADY_LOADED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class ClaimLoadResult:
    """The outcome of loading one file, and enough detail to act on it."""

    outcome: ClaimLoadOutcome
    archived: ArchivedFile
    raw_file_id: uuid.UUID
    claims_loaded: int = 0
    lines_loaded: int = 0
    resubmissions_chained: int = 0
    quarantine_assertion: str | None = None
    quarantine_reason: str | None = None

    @property
    def quarantined(self) -> bool:
        return self.outcome is ClaimLoadOutcome.QUARANTINED


def _ensure_payer(session: Session, *, payer_id: str, name: str) -> str:
    """Find or create the payer this claim names (PD-8).

    Keyed on the identifier the claim carries in `NM1*PR` element 9, never on
    the name in element 3. The name is recorded for operators and reconciled
    across spellings by M1.7's alias matching.
    """
    identifier = payer_id.strip() or "UNKNOWN"
    if session.get(Payer, identifier) is None:
        session.add(Payer(payer_id=identifier, name=name.strip() or identifier))
        # Flushed here rather than left to the unit of work: the claim inserted
        # immediately after carries a foreign key to this row, and SQLAlchemy
        # has no way to know that ordering from the object graph alone.
        session.flush()
    # M1.7: record the spelling this file used, unconfirmed.
    record_alias(session, payer_id=identifier, name=name)
    return identifier


def _quarantine(
    session: Session,
    raw_file: RawFile,
    *,
    assertion: str,
    reason: str,
    context: dict[str, object],
    actor_id: str,
) -> None:
    """Mark a file quarantined, with the assertion it failed (DR-10).

    No CORE rows for a quarantined file, not even the claims that parsed
    cleanly. Partial loads are the failure mode this exists to prevent.
    """
    raw_file.status = str(RawFileStatus.QUARANTINED)
    raw_file.quarantine_assertion = assertion
    raw_file.quarantine_reason = reason
    raw_file.quarantine_context = context
    raw_file.processed_at = datetime.now(UTC)

    record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="claim.quarantined",
        entity_type="raw_file",
        entity_id=raw_file.raw_file_id,
        after_state={"assertion": assertion, "status": str(RawFileStatus.QUARANTINED)},
    )
    log.error(
        "claim.quarantined",
        assertion=assertion,
        content_hash=raw_file.content_hash[:12],
        original_filename=raw_file.original_filename,
    )


def _find_claim(session: Session, *, client_id: uuid.UUID, submitter_claim_id: str) -> Claim | None:
    """The claim a client has already loaded under this CLM01, if any."""
    return session.scalar(
        select(Claim).where(
            Claim.client_id == client_id,
            Claim.submitter_claim_id == submitter_claim_id,
        )
    )


def _modifiers(parsed: ParsedClaim, index: int) -> dict[str, str | None]:
    """Spread the parsed modifier tuple across the four positional columns.

    DR-05: ordered positional columns, never a set. Position carries meaning,
    so modifier 1 missing while modifier 2 is populated is a different claim
    from the reverse.
    """
    line = parsed.lines[index]
    return {
        f"modifier_{position}": (
            line.modifiers[position - 1] if position <= len(line.modifiers) else None
        )
        for position in range(1, MAX_MODIFIERS + 1)
    }


@dataclass(frozen=True, slots=True)
class _PlannedClaim:
    """One claim resolved against the chain, before anything is written.

    Carries the same three attribute names as `Claim` -- `claim_id`,
    `root_claim_id`, `submission_attempt` -- so a parent found in this batch
    and a parent loaded from the database are read the same way.
    """

    parsed: ParsedClaim
    claim_id: uuid.UUID
    original_claim_id: uuid.UUID | None
    root_claim_id: uuid.UUID
    submission_attempt: int


def load_claim_file(
    session: Session,
    raw: bytes,
    *,
    client_id: uuid.UUID,
    original_filename: str,
    archive_root: Path | str,
    limits: Limits | None = None,
    actor_id: str = "system:claim_loader",
    received_at: datetime | None = None,
) -> ClaimLoadResult:
    """Archive, parse, assert, then load one 837 file.

    Everything after the archive write runs in the caller's transaction, so a
    load that fails partway leaves no CORE rows and a `raw_file` row that says
    why.
    """
    archived = archive(
        raw,
        original_filename=original_filename,
        archive_root=archive_root,
        received_at=received_at,
    )

    existing = session.scalar(select(RawFile).where(RawFile.content_hash == archived.content_hash))
    if existing is not None:
        # PD-14: the same bytes again are recognised rather than reloaded.
        # Recognising the file is what makes this idempotent; the load itself
        # is not repeatable, because every row has a generated primary key.
        log.info(
            "claim.already_processed",
            content_hash=archived.short_hash,
            status=existing.status,
        )
        return ClaimLoadResult(
            outcome=(
                ClaimLoadOutcome.QUARANTINED
                if existing.status == str(RawFileStatus.QUARANTINED)
                else ClaimLoadOutcome.ALREADY_LOADED
            ),
            archived=archived,
            raw_file_id=existing.raw_file_id,
            quarantine_assertion=existing.quarantine_assertion,
            quarantine_reason=existing.quarantine_reason,
        )

    raw_file = RawFile(
        client_id=client_id,
        content_hash=archived.content_hash,
        byte_size=archived.byte_size,
        storage_path=archived.relative_path,
        original_filename=archived.original_filename,
        transaction_kind=str(archived.transaction_kind),
        received_at=archived.received_at,
        status=str(RawFileStatus.ARCHIVED),
    )
    session.add(raw_file)
    session.flush()

    def quarantined(assertion: str, reason: str, context: dict[str, object]) -> ClaimLoadResult:
        _quarantine(
            session,
            raw_file,
            assertion=assertion,
            reason=reason,
            context=context,
            actor_id=actor_id,
        )
        return ClaimLoadResult(
            outcome=ClaimLoadOutcome.QUARANTINED,
            archived=archived,
            raw_file_id=raw_file.raw_file_id,
            quarantine_assertion=assertion,
            quarantine_reason=reason,
        )

    # --- parse --------------------------------------------------------------
    try:
        interchange = read_interchange(raw, limits=limits)
        submissions: tuple[ParsedSubmission, ...] = tuple(
            parse_837(transaction, interchange.separators)
            for transaction in interchange.transaction_sets("837")
        )
    except QuarantineError as exc:
        return quarantined(
            ASSERTION_PARSEABLE,
            exc.detail,
            {"code": exc.code, **{k: str(v) for k, v in exc.context.items()}},
        )
    except PlatformError as exc:
        # A named error that is not a QuarantineError still means the file
        # cannot be loaded. Quarantining contains it; letting it propagate
        # would abort a batch over one bad file.
        return quarantined(
            ASSERTION_PARSEABLE,
            exc.detail,
            {"code": exc.code, **{k: str(v) for k, v in exc.context.items()}},
        )

    parsed_claims = [claim for submission in submissions for claim in submission.claims]
    if not parsed_claims:
        return quarantined(
            ASSERTION_CONTAINS_CLAIM,
            "the interchange parsed but contains no 837 claim",
            {"transaction_kind": str(archived.transaction_kind)},
        )

    # --- assert -------------------------------------------------------------
    for parsed in parsed_claims:
        if not parsed.charges_reconcile:
            return quarantined(
                ASSERTION_CHARGES_RECONCILE,
                "a claim's stated total disagrees with the sum of its service lines",
                {
                    "claim_id": parsed.claim_id,
                    "total_charge": str(parsed.total_charge),
                    "line_charge_total": str(parsed.line_charge_total),
                    "lines": len(parsed.lines),
                },
            )
        unpointed = parsed.lines_without_diagnosis_pointer
        if unpointed:
            return quarantined(
                ASSERTION_DIAGNOSIS_POINTER,
                "BR-M6-01: a service line carries no diagnosis pointer",
                {"claim_id": parsed.claim_id, "lines": list(unpointed)},
            )

    # --- plan ---------------------------------------------------------------
    # Every claim is resolved and validated before any row is written. Two
    # reasons, both found by the tests rather than by design:
    #
    # A quarantine partway through a single pass left the claims added before
    # it pending in the session, so a file marked QUARANTINED still wrote rows
    # -- the partial load DR-10 exists to prevent, wearing the right status.
    #
    # And the session runs with `autoflush=False` (PD-2 depends on nothing
    # reaching the database outside an explicit transaction boundary), so a
    # resubmission could not see an original added moments earlier in the same
    # file. Resolving within the batch first fixes that without flushing
    # half-validated rows to find out.
    planned: list[_PlannedClaim] = []
    by_submitter_id: dict[str, _PlannedClaim] = {}

    for parsed in parsed_claims:
        if parsed.claim_id in by_submitter_id:
            # The same CLM01 twice in one file is the same claim stated twice.
            continue
        if _find_claim(session, client_id=client_id, submitter_claim_id=parsed.claim_id):
            # Already loaded from another file. Nothing to add, and not an
            # error: a resend under a new envelope is a normal thing to see.
            continue

        parent: _PlannedClaim | Claim | None = None
        if parsed.is_resubmission:
            parent = by_submitter_id.get(parsed.original_claim_id) or _find_claim(
                session, client_id=client_id, submitter_claim_id=parsed.original_claim_id
            )
            if parent is None:
                return quarantined(
                    ASSERTION_KNOWN_ORIGINAL,
                    "a resubmission names an original claim that is not loaded",
                    {
                        "claim_id": parsed.claim_id,
                        "original_claim_id": parsed.original_claim_id,
                    },
                )

        claim_id = uuid.uuid4()
        plan = _PlannedClaim(
            parsed=parsed,
            claim_id=claim_id,
            # BR-DATA-03: the chain as an edge, and already resolved. `root`
            # comes from the parent's root rather than the parent, so a
            # three-deep chain roots at the first submission and not at the
            # middle one.
            original_claim_id=parent.claim_id if parent is not None else None,
            root_claim_id=parent.root_claim_id if parent is not None else claim_id,
            submission_attempt=(parent.submission_attempt + 1) if parent is not None else 1,
        )
        planned.append(plan)
        by_submitter_id[parsed.claim_id] = plan

    # --- load ---------------------------------------------------------------
    lines_loaded = 0
    chained = 0

    for plan in planned:
        parsed = plan.parsed
        session.add(
            Claim(
                claim_id=plan.claim_id,
                client_id=client_id,
                raw_file_id=raw_file.raw_file_id,
                submitter_claim_id=parsed.claim_id,
                original_claim_id=plan.original_claim_id,
                root_claim_id=plan.root_claim_id,
                submission_attempt=plan.submission_attempt,
                claim_frequency_code=parsed.claim_frequency_code or "1",
                payer_id=_ensure_payer(session, payer_id=parsed.payer_id, name=parsed.payer_name),
                payer_responsibility=parsed.payer_responsibility or "P",
                claim_filing_code=parsed.claim_filing_code,
                billing_npi=parsed.billing_provider.npi,
                billing_tax_id=parsed.billing_provider.tax_id,
                subscriber_member_id=parsed.subscriber.member_id,
                place_of_service=parsed.place_of_service,
                date_of_service=parsed.service_date,
                total_charge=parsed.total_charge,
                prior_auth_number=parsed.prior_auth_number,
                referral_number=parsed.referral_number,
                diagnoses=list(parsed.diagnoses),
            )
        )
        if plan.original_claim_id is not None:
            chained += 1

        for index, line in enumerate(parsed.lines):
            session.add(
                ClaimLine(
                    client_id=client_id,
                    claim_id=plan.claim_id,
                    line_number=line.line_number,
                    procedure_code=line.procedure_code,
                    units=line.units,
                    charge_amount=line.charge_amount,
                    diagnosis_pointers=list(line.diagnosis_pointers),
                    service_date=line.service_date,
                    **_modifiers(parsed, index),
                )
            )
            lines_loaded += 1

    claims_loaded = len(planned)

    raw_file.status = str(RawFileStatus.LOADED)
    raw_file.processed_at = datetime.now(UTC)
    session.flush()

    record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="claim.loaded",
        entity_type="raw_file",
        entity_id=raw_file.raw_file_id,
        after_state={
            "claims": claims_loaded,
            "lines": lines_loaded,
            "resubmissions_chained": chained,
        },
    )
    log.info(
        "claim.loaded",
        content_hash=archived.short_hash,
        claims=claims_loaded,
        lines=lines_loaded,
        resubmissions_chained=chained,
    )

    return ClaimLoadResult(
        outcome=ClaimLoadOutcome.LOADED,
        archived=archived,
        raw_file_id=raw_file.raw_file_id,
        claims_loaded=claims_loaded,
        lines_loaded=lines_loaded,
        resubmissions_chained=chained,
    )
