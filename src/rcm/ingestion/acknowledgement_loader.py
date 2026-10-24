"""Load 999 and 277CA acknowledgements into CORE (M1.6).

Same order as the other loaders -- archive, recognise, parse, assert, load --
so the three differ only where the transactions differ.

What is specific here is PD-4. A rejection and a denial are both "the claim
was not paid" and they behave oppositely, so the whole job of this loader is
to land rejections somewhere a denial query cannot reach them. It writes to
`claim_acknowledgement`; denials are derived from the remittance tables; the
two never meet.

**An unrecognised status category stops the load.** A 277 category outside
the accepted and rejected sets has no verdict this platform can read, and
guessing one means a claim silently acquires an outcome nobody chose (PD-18).
The file is quarantined under `IR-09` with the category named, which is
recoverable -- the bytes are in RAW and the file loads once the code list is
extended.

**A 999 rejection names the whole transmission, not a claim.** AK2 carries
the transaction set control number rather than any claim id, so the rejection
is recorded against that control number. Attributing it to individual claims
would mean inventing a mapping the file does not contain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.domain.acknowledgement import (
    AcknowledgementOutcome,
    AcknowledgementSource,
    ClaimAcknowledgement,
)
from rcm.domain.remittance import RawFile, RawFileStatus
from rcm.ingestion.archive import ArchivedFile, archive
from rcm.ingestion.x12 import Limits, parse_271, parse_277, parse_999, read_interchange
from rcm.platform.audit import ActorType, record_audit_event
from rcm.platform.errors import PlatformError, QuarantineError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

ASSERTION_PARSEABLE: Final = "IR-01: file parses as X12"
ASSERTION_CONTAINS_ACKNOWLEDGEMENT: Final = "IR-09: file contains a 999, 277 or 271 transaction"
ASSERTION_KNOWN_STATUS_CATEGORY: Final = (
    "IR-10: every claim status category is one the platform reads"
)


class AcknowledgementLoadOutcome(StrEnum):
    LOADED = "LOADED"
    ALREADY_LOADED = "ALREADY_LOADED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class AcknowledgementLoadResult:
    outcome: AcknowledgementLoadOutcome
    archived: ArchivedFile
    raw_file_id: uuid.UUID
    accepted: int = 0
    rejected: int = 0
    #: 271s are parsed and reported but not stored: eligibility belongs to
    #: M3's module, and a table nothing reads is a table that rots.
    eligibility_responses: int = 0
    quarantine_assertion: str | None = None
    quarantine_reason: str | None = None

    @property
    def quarantined(self) -> bool:
        return self.outcome is AcknowledgementLoadOutcome.QUARANTINED


def _quarantine(
    session: Session,
    raw_file: RawFile,
    *,
    assertion: str,
    reason: str,
    context: dict[str, object],
    actor_id: str,
) -> None:
    raw_file.status = str(RawFileStatus.QUARANTINED)
    raw_file.quarantine_assertion = assertion
    raw_file.quarantine_reason = reason
    raw_file.quarantine_context = context
    raw_file.processed_at = datetime.now(UTC)

    record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="acknowledgement.quarantined",
        entity_type="raw_file",
        entity_id=raw_file.raw_file_id,
        after_state={"assertion": assertion, "status": str(RawFileStatus.QUARANTINED)},
    )
    log.error(
        "acknowledgement.quarantined",
        assertion=assertion,
        content_hash=raw_file.content_hash[:12],
        original_filename=raw_file.original_filename,
    )


def load_acknowledgement_file(
    session: Session,
    raw: bytes,
    *,
    client_id: uuid.UUID,
    original_filename: str,
    archive_root: Path | str,
    limits: Limits | None = None,
    actor_id: str = "system:acknowledgement_loader",
    received_at: datetime | None = None,
) -> AcknowledgementLoadResult:
    """Archive, parse, assert, then load one acknowledgement file."""
    archived = archive(
        raw,
        original_filename=original_filename,
        archive_root=archive_root,
        received_at=received_at,
    )

    existing = session.scalar(select(RawFile).where(RawFile.content_hash == archived.content_hash))
    if existing is not None:
        log.info(
            "acknowledgement.already_processed",
            content_hash=archived.short_hash,
            status=existing.status,
        )
        return AcknowledgementLoadResult(
            outcome=(
                AcknowledgementLoadOutcome.QUARANTINED
                if existing.status == str(RawFileStatus.QUARANTINED)
                else AcknowledgementLoadOutcome.ALREADY_LOADED
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

    def quarantined(
        assertion: str, reason: str, context: dict[str, object]
    ) -> AcknowledgementLoadResult:
        _quarantine(
            session,
            raw_file,
            assertion=assertion,
            reason=reason,
            context=context,
            actor_id=actor_id,
        )
        return AcknowledgementLoadResult(
            outcome=AcknowledgementLoadOutcome.QUARANTINED,
            archived=archived,
            raw_file_id=raw_file.raw_file_id,
            quarantine_assertion=assertion,
            quarantine_reason=reason,
        )

    # --- parse --------------------------------------------------------------
    try:
        interchange = read_interchange(raw, limits=limits)
        syntax = tuple(
            parse_999(t, interchange.separators) for t in interchange.transaction_sets("999")
        )
        statuses = tuple(
            parse_277(t, interchange.separators) for t in interchange.transaction_sets("277")
        )
        eligibility = tuple(
            parse_271(t, interchange.separators) for t in interchange.transaction_sets("271")
        )
    except (QuarantineError, PlatformError) as exc:
        return quarantined(
            ASSERTION_PARSEABLE,
            exc.detail,
            {"code": exc.code, **{k: str(v) for k, v in exc.context.items()}},
        )

    if not (syntax or statuses or eligibility):
        return quarantined(
            ASSERTION_CONTAINS_ACKNOWLEDGEMENT,
            "the interchange parsed but carries no acknowledgement transaction",
            {"transaction_kind": str(archived.transaction_kind)},
        )

    # --- assert -------------------------------------------------------------
    for response in statuses:
        unknown = response.unknown_categories
        if unknown:
            return quarantined(
                ASSERTION_KNOWN_STATUS_CATEGORY,
                "a claim status category is outside the accepted and rejected code lists",
                {"categories": list(unknown)},
            )

    # --- load ---------------------------------------------------------------
    rows: list[ClaimAcknowledgement] = []
    seen: set[tuple[str, str, str]] = set()

    def add(
        *,
        claim_id: str,
        source: AcknowledgementSource,
        outcome: AcknowledgementOutcome,
        category_code: str = "",
        status_code: str = "",
        description: str = "",
        payer_claim_number: str = "",
        effective_date: date | None = None,
    ) -> None:
        # One file saying the same thing about the same claim twice is one
        # fact, and the table's uniqueness constraint agrees.
        key = (claim_id, str(source), category_code)
        if key in seen:
            return
        seen.add(key)
        rows.append(
            ClaimAcknowledgement(
                client_id=client_id,
                raw_file_id=raw_file.raw_file_id,
                claim_id=claim_id,
                payer_claim_number=payer_claim_number,
                source=str(source),
                outcome=str(outcome),
                category_code=category_code,
                status_code=status_code,
                description=description,
                effective_date=effective_date,
                received_at=archived.received_at,
            )
        )

    for acknowledgement in syntax:
        # A 999 names the transmission, not a claim. Recording it against the
        # transaction set control number keeps the fact without inventing a
        # claim mapping the file does not contain.
        for transaction in acknowledgement.transactions or ():
            add(
                claim_id=transaction.control_number or acknowledgement.group_control_number,
                source=AcknowledgementSource.SYNTAX_999,
                outcome=(
                    AcknowledgementOutcome.ACCEPTED
                    if transaction.accepted
                    else AcknowledgementOutcome.REJECTED
                ),
                description="; ".join(
                    f"{e.segment_id}@{e.segment_position}:{e.element_error_code or e.error_code}"
                    for e in transaction.errors
                ),
            )
        if not acknowledgement.transactions:
            add(
                claim_id=acknowledgement.group_control_number or "UNKNOWN",
                source=AcknowledgementSource.SYNTAX_999,
                outcome=(
                    AcknowledgementOutcome.ACCEPTED
                    if acknowledgement.accepted
                    else AcknowledgementOutcome.REJECTED
                ),
            )

    for response in statuses:
        for status in response.statuses:
            add(
                claim_id=status.claim_id or "UNKNOWN",
                source=AcknowledgementSource.CLAIM_277CA,
                outcome=(
                    AcknowledgementOutcome.ACCEPTED
                    if status.accepted
                    else AcknowledgementOutcome.REJECTED
                ),
                category_code=status.category_code,
                status_code=status.status_code,
                description=status.description,
                payer_claim_number=status.payer_claim_number,
                effective_date=status.effective_date,
            )

    for row in rows:
        session.add(row)

    accepted = sum(1 for r in rows if r.outcome == str(AcknowledgementOutcome.ACCEPTED))
    rejected = len(rows) - accepted

    raw_file.status = str(RawFileStatus.LOADED)
    raw_file.processed_at = datetime.now(UTC)
    session.flush()

    record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="acknowledgement.loaded",
        entity_type="raw_file",
        entity_id=raw_file.raw_file_id,
        after_state={"accepted": accepted, "rejected": rejected},
    )
    log.info(
        "acknowledgement.loaded",
        content_hash=archived.short_hash,
        accepted=accepted,
        rejected=rejected,
    )

    return AcknowledgementLoadResult(
        outcome=AcknowledgementLoadOutcome.LOADED,
        archived=archived,
        raw_file_id=raw_file.raw_file_id,
        accepted=accepted,
        rejected=rejected,
        eligibility_responses=len(eligibility),
    )
