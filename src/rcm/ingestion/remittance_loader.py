"""Archive, parse, validate, load -- in that order (M1.3, M1.4, M1.8).

The order is the design. PD-9 requires the archive write to happen before the
parse, and DR-10 requires a file that fails a data-quality gate to be
quarantined *unparsed into CORE* rather than partially loaded. Both are easy to
get backwards, and both are invisible once wrong: a partially loaded file looks
exactly like a smaller file.

    1. archive   bytes land in RAW, immutably, before anything reads them
    2. recognise an identical file already loaded is a no-op, not a duplicate
    3. parse     structure and envelope integrity
    4. assert    the file reconciles against its own stated total
    5. load      all of it, in one transaction, or none of it

Step 4 is the one worth dwelling on. An 835 states a payment total in BPR02 and
then itemises it. If the itemisation does not add up to the total, the file is
not "mostly right" -- it is a file we do not understand, and posting the part
we do understand puts wrong numbers in a ledger that money is reconciled
against. BR-M9-01 says so, and this is where it is enforced.

Everything after step 1 runs inside a single database transaction. A load that
fails halfway leaves no rows and a `raw_file` row that says why, because both
the rows and the status come from the same transaction.
"""

from __future__ import annotations

import collections
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.domain.payer import Payer
from rcm.domain.remittance import (
    ClaimPayment,
    ProviderAdjustment,
    RawFile,
    RawFileStatus,
    Remittance,
    RemittanceAdjustment,
    RemittanceServiceLine,
)
from rcm.ingestion.archive import ArchivedFile, archive
from rcm.ingestion.x12 import Limits, parse_835, read_interchange
from rcm.ingestion.x12 import Remittance as ParsedRemittance
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.errors import BalanceAssertionError, PlatformError, QuarantineError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

#: Assertion names recorded on a quarantined file. These are the strings an
#: operator sees and an alert routes on, so they are stable identifiers rather
#: than prose -- DR-10 requires the *specific* failing assertion, and "parse
#: error" routes nowhere.
ASSERTION_BALANCE: Final = "BR-M9-01: BPR02 == sum(CLP04) - sum(PLB)"
ASSERTION_SERVICE_LINE_BALANCE: Final = "IR-02: SVC02 == SVC03 + sum(CAS)"
ASSERTION_PARSEABLE: Final = "IR-01: file parses as X12"
ASSERTION_CONTAINS_REMITTANCE: Final = "IR-03: file contains at least one 835"
ASSERTION_UNIQUE_CONTROL_NUMBER: Final = "IR-04: ST02 is unique within an interchange"


class LoadOutcome(StrEnum):
    """What happened to one inbound file."""

    LOADED = "LOADED"
    #: Already in RAW with the same bytes, and already loaded. Nothing to do.
    ALREADY_LOADED = "ALREADY_LOADED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class LoadResult:
    """The outcome of loading one file, and enough detail to act on it."""

    outcome: LoadOutcome
    archived: ArchivedFile
    raw_file_id: uuid.UUID
    remittances_loaded: int = 0
    claims_loaded: int = 0
    service_lines_loaded: int = 0
    adjustments_loaded: int = 0
    quarantine_assertion: str | None = None
    quarantine_reason: str | None = None

    @property
    def quarantined(self) -> bool:
        return self.outcome is LoadOutcome.QUARANTINED


def _ensure_payer(session: Session, *, payer_identifier: str, name: str, fallback: str) -> str:
    """Find or create the payer this remittance names (PD-8).

    Created on first sight rather than requiring pre-registration, because a
    payer appearing in a file the platform has already accepted is a fact, and
    refusing the file would lose real money over a registry gap. The name is
    recorded but never used as the key; M1.7's alias matching is what reconciles
    the several spellings of one payer.

    An existing payer's name is **not** overwritten. The first spelling seen is
    no more authoritative than the next, and a name that changes under a payer
    between loads makes every report that quoted it unreproducible.
    """
    # PD-8. The name is never the key, not even when the identifier is
    # missing: falling back to the interchange sender keeps the key an
    # identifier, where falling back to the name would silently create one
    # payer per spelling -- the exact failure the directive exists to prevent.
    identifier = payer_identifier.strip() or fallback.strip() or "UNKNOWN"
    existing = session.get(Payer, identifier)
    if existing is None:
        session.add(Payer(payer_id=identifier, name=name.strip() or identifier))
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

    No CORE rows are written for a quarantined file -- not "the claims that
    parsed", not "the balanced ones". A file that fails a quality gate is a
    file whose contents are not understood, and loading the part that happens
    to be readable is how a ledger acquires numbers nobody can account for.
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
        action="remittance.quarantined",
        entity_type="raw_file",
        entity_id=raw_file.raw_file_id,
        after_state={"assertion": assertion, "status": str(RawFileStatus.QUARANTINED)},
    )
    # A named alert, not a generic one. DR-10's whole point is that an
    # operator can tell what failed without opening the file.
    log.error(
        "remittance.quarantined",
        assertion=assertion,
        content_hash=raw_file.content_hash[:12],
        original_filename=raw_file.original_filename,
    )


def _load_one_remittance(
    session: Session,
    parsed: ParsedRemittance,
    *,
    raw_file: RawFile,
    client_id: uuid.UUID,
    payer_fallback: str,
) -> tuple[int, int, int]:
    """Write one parsed 835 into CORE. Returns (claims, lines, adjustments)."""
    payer_id = _ensure_payer(
        session,
        payer_identifier=parsed.payer_identifier,
        name=parsed.payer_name,
        fallback=payer_fallback,
    )

    remittance = Remittance(
        client_id=client_id,
        raw_file_id=raw_file.raw_file_id,
        payer_id=payer_id,
        payment_amount=parsed.payment_amount,
        payment_date=parsed.payment_date,
        trace_number=parsed.trace_number,
        transaction_control_number=parsed.transaction_control_number,
        payee_npi=parsed.payee_npi,
        claim_payment_total=parsed.claim_payment_total,
        provider_adjustment_total=parsed.provider_adjustment_total,
    )
    session.add(remittance)
    session.flush()  # assign remittance_id for the children below

    claims = lines = adjustments = 0

    for parsed_claim in parsed.claims:
        claim = ClaimPayment(
            client_id=client_id,
            remittance_id=remittance.remittance_id,
            claim_id=parsed_claim.claim_id,
            payer_control_number=parsed_claim.payer_control_number,
            status_code=parsed_claim.status_code,
            filing_indicator=parsed_claim.filing_indicator,
            total_charge=parsed_claim.total_charge,
            total_paid=parsed_claim.total_paid,
            patient_responsibility=parsed_claim.patient_responsibility,
            service_date=parsed_claim.service_date,
            member_id=parsed_claim.member_id,
            rendering_provider_npi=parsed_claim.rendering_provider_npi,
        )
        session.add(claim)
        session.flush()
        claims += 1

        for sequence, parsed_adjustment in enumerate(parsed_claim.claim_adjustments, start=1):
            session.add(
                RemittanceAdjustment(
                    client_id=client_id,
                    claim_payment_id=claim.claim_payment_id,
                    service_line_id=None,
                    cas_group_code=parsed_adjustment.group,
                    reason_code=parsed_adjustment.reason_code,
                    amount=parsed_adjustment.amount,
                    quantity=parsed_adjustment.quantity,
                    claim_level=True,
                    sequence=sequence,
                )
            )
            adjustments += 1

        for line_number, parsed_line in enumerate(parsed_claim.service_lines, start=1):
            line = RemittanceServiceLine(
                client_id=client_id,
                claim_payment_id=claim.claim_payment_id,
                line_number=line_number,
                procedure_code=parsed_line.procedure_code,
                modifiers=list(parsed_line.modifiers),
                charge_amount=parsed_line.charge_amount,
                paid_amount=parsed_line.paid_amount,
                allowed_amount=parsed_line.allowed_amount,
                units=parsed_line.units,
                service_date=parsed_line.service_date,
                remark_codes=list(parsed_line.remark_codes),
            )
            session.add(line)
            session.flush()
            lines += 1

            # PD-6: one row per reason, not one per CAS segment.
            for sequence, parsed_adjustment in enumerate(parsed_line.adjustments, start=1):
                session.add(
                    RemittanceAdjustment(
                        client_id=client_id,
                        claim_payment_id=claim.claim_payment_id,
                        service_line_id=line.service_line_id,
                        cas_group_code=parsed_adjustment.group,
                        reason_code=parsed_adjustment.reason_code,
                        amount=parsed_adjustment.amount,
                        quantity=parsed_adjustment.quantity,
                        claim_level=False,
                        sequence=sequence,
                    )
                )
                adjustments += 1

    for sequence, parsed_plb in enumerate(parsed.provider_adjustments, start=1):
        session.add(
            ProviderAdjustment(
                client_id=client_id,
                remittance_id=remittance.remittance_id,
                provider_identifier=parsed_plb.provider_id,
                fiscal_period_end=parsed_plb.fiscal_period_end,
                reason_code=parsed_plb.reason_code,
                reference=parsed_plb.reference,
                amount=parsed_plb.amount,
                sequence=sequence,
            )
        )

    return claims, lines, adjustments


def load_remittance_file(
    session: Session,
    raw: bytes,
    *,
    client_id: uuid.UUID,
    original_filename: str,
    archive_root: Path | str,
    limits: Limits | None = None,
    actor_id: str = "system:remittance_loader",
    received_at: datetime | None = None,
) -> LoadResult:
    """Archive, parse, assert and load one inbound remittance file.

    The caller supplies the session so that the whole load -- CORE rows, the
    `raw_file` status and the audit event -- commits or rolls back together
    (PD-2). A loader that managed its own transaction could leave a file marked
    LOADED with none of its rows present.
    """
    archived = archive(
        raw,
        original_filename=original_filename,
        archive_root=archive_root,
        received_at=received_at,
    )

    existing = session.scalar(select(RawFile).where(RawFile.content_hash == archived.content_hash))
    if existing is not None:
        # Reprocessing the same bytes changes nothing. This is the M1 gate's
        # idempotency requirement, and it is satisfied by recognising the file
        # rather than by making the load itself repeatable -- which it is not,
        # because every row has a generated primary key.
        log.info(
            "remittance.already_processed",
            content_hash=archived.short_hash,
            status=existing.status,
        )
        return LoadResult(
            outcome=(
                LoadOutcome.QUARANTINED
                if existing.status == str(RawFileStatus.QUARANTINED)
                else LoadOutcome.ALREADY_LOADED
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

    def quarantined(assertion: str, reason: str, context: dict[str, object]) -> LoadResult:
        _quarantine(
            session,
            raw_file,
            assertion=assertion,
            reason=reason,
            context=context,
            actor_id=actor_id,
        )
        return LoadResult(
            outcome=LoadOutcome.QUARANTINED,
            archived=archived,
            raw_file_id=raw_file.raw_file_id,
            quarantine_assertion=assertion,
            quarantine_reason=reason,
        )

    # --- parse ------------------------------------------------------------
    interchange_sender = ""
    try:
        interchange = read_interchange(raw, limits=limits)
        interchange_sender = interchange.sender_id
        parsed_remittances = tuple(
            parse_835(transaction, interchange.separators)
            for transaction in interchange.transaction_sets("835")
        )
    except QuarantineError as exc:
        return quarantined(
            ASSERTION_PARSEABLE,
            exc.detail,
            {"code": exc.code, **{k: str(v) for k, v in exc.context.items()}},
        )
    except PlatformError as exc:
        # A named error that is not a QuarantineError subclass still means the
        # file cannot be loaded. Quarantining is the safe outcome; letting it
        # propagate would abort a batch over one bad file.
        return quarantined(
            ASSERTION_PARSEABLE,
            exc.detail,
            {"code": exc.code, **{k: str(v) for k, v in exc.context.items()}},
        )

    if not parsed_remittances:
        return quarantined(
            ASSERTION_CONTAINS_REMITTANCE,
            "the interchange parsed but contains no 835 transaction set",
            {"transaction_kind": str(archived.transaction_kind)},
        )

    # --- assert ------------------------------------------------------------
    # ST02 is what distinguishes two remittances inside one file, and
    # `uq_remittance_raw_file_id_transaction_control_number` enforces that. A
    # file whose transaction sets repeat a control number is malformed, and
    # without this check the uniqueness violation surfaced as an unhandled
    # IntegrityError out of the load below -- which rolls back the whole
    # transaction, including the `raw_file` row. The bytes stayed on disk but
    # nothing recorded why they failed, so the file was neither loaded nor
    # quarantined: PD-9 and DR-10 both broken by a file a payer could plausibly
    # send. Asserted here instead, where it can name itself.
    repeated = sorted(
        control
        for control, count in collections.Counter(
            parsed.transaction_control_number for parsed in parsed_remittances
        ).items()
        if count > 1
    )
    if repeated:
        return quarantined(
            ASSERTION_UNIQUE_CONTROL_NUMBER,
            "two transaction sets in this interchange share a control number",
            {"repeated": repeated, "transaction_sets": len(parsed_remittances)},
        )

    for parsed in parsed_remittances:
        if not parsed.balances:
            return quarantined(
                ASSERTION_BALANCE,
                BalanceAssertionError.detail,
                {
                    "stated_payment": str(parsed.payment_amount),
                    "claim_payment_total": str(parsed.claim_payment_total),
                    "provider_adjustment_total": str(parsed.provider_adjustment_total),
                    "expected_payment": str(parsed.expected_payment),
                    "discrepancy": str(parsed.balance_discrepancy),
                    "claims": len(parsed.claims),
                },
            )

        unbalanced = parsed.unbalanced_service_lines
        if unbalanced:
            # A line that does not reconcile means an adjustment was dropped or
            # misread. The claim totals can still look right while this is
            # true, which is why it is checked separately.
            return quarantined(
                ASSERTION_SERVICE_LINE_BALANCE,
                "one or more service lines do not reconcile against their adjustments",
                {
                    "lines": [f"{claim}:{procedure}" for claim, procedure in unbalanced[:20]],
                    "count": len(unbalanced),
                },
            )

    # --- load --------------------------------------------------------------
    claims = lines = adjustments = 0
    for parsed in parsed_remittances:
        loaded_claims, loaded_lines, loaded_adjustments = _load_one_remittance(
            session,
            parsed,
            raw_file=raw_file,
            client_id=client_id,
            payer_fallback=interchange_sender,
        )
        claims += loaded_claims
        lines += loaded_lines
        adjustments += loaded_adjustments

    raw_file.status = str(RawFileStatus.LOADED)
    raw_file.processed_at = datetime.now(UTC)

    record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="remittance.loaded",
        entity_type="raw_file",
        entity_id=raw_file.raw_file_id,
        after_state={
            "remittances": len(parsed_remittances),
            "claims": claims,
            "service_lines": lines,
            "adjustments": adjustments,
        },
    )
    log.info(
        "remittance.loaded",
        content_hash=archived.short_hash,
        claims=claims,
        service_lines=lines,
        adjustments=adjustments,
    )

    return LoadResult(
        outcome=LoadOutcome.LOADED,
        archived=archived,
        raw_file_id=raw_file.raw_file_id,
        remittances_loaded=len(parsed_remittances),
        claims_loaded=claims,
        service_lines_loaded=lines,
        adjustments_loaded=adjustments,
    )
