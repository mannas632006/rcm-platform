"""CORE remittance tables (M1.8).

The schema is where PD-3 and PD-6 stop being conventions and become things the
database refuses to store wrongly. A test can be deleted; a `NOT NULL` cannot
be worked around by a future loader that was written in a hurry.

**PD-6, service-line grain, one row per adjustment reason.** The grain is the
table structure, not a rule applied to it: `remittance_adjustment` exists
precisely so that a line with three CAS reasons is three rows. There is
deliberately no unique constraint on (service_line, reason_code) -- a payer can
legitimately send the same reason twice with different amounts, and a
constraint forbidding it would force a loader to merge them, which is the
collapsing PD-6 prohibits.

**PD-3, a reason code never travels without its group.** `cas_group_code` is
`NOT NULL` with a `CHECK` against the five valid values. CO-45 and PR-45 are
opposite outcomes -- a contractual write-off the patient may never be billed
for, and an amount they owe -- and the group is the only thing distinguishing
them.

**PD-20, client scoping at the query layer.** `client_id` is carried on every
table here rather than reached through a join to the remittance. That is
denormalisation chosen on purpose: `scoped_query` filters the entity being
queried, and a scope that depends on a join is a scope a future query can omit
by not writing the join. The redundant column is cheaper than that risk.

**PD-9/PD-10, everything traces to RAW.** Every remittance references the
`raw_file` it was parsed from, so any row here can be rebuilt from bytes the
platform still holds.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base

#: Money: exact, never float. 12 digits with 2 decimals holds a ten-million
#: dollar claim, which is well beyond anything a single service line carries.
_MONEY = Numeric(12, 2)

#: Units can be fractional (anaesthesia time, drug quantities).
_QUANTITY = Numeric(12, 3)

#: The five CAS group codes, X12 code list 1033. Enforced as a CHECK rather
#: than an enum type so that adding one later is a migration rather than a
#: type alteration, and so the constraint names itself in the error.
VALID_CAS_GROUP_CODES: tuple[str, ...] = ("CO", "PR", "OA", "PI", "CR")


class RawFileStatus(StrEnum):
    """What happened to an archived file.

    `QUARANTINED` is terminal until a human acts. DR-10 requires a quarantined
    file to be named with the assertion it failed and never partially loaded,
    so there is no `PARTIALLY_LOADED` state -- the absence of one is the point.
    """

    ARCHIVED = "ARCHIVED"
    LOADED = "LOADED"
    QUARANTINED = "QUARANTINED"


class RawFile(Base):
    """One inbound file, archived before it was parsed (PD-9).

    The row is written in the same transaction as the load it describes, so a
    file can never appear LOADED without its remittances or vice versa.
    """

    __tablename__ = "raw_file"

    raw_file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    #: SHA-256 of the bytes as received. Unique: re-archiving the same file is
    #: recognised rather than duplicated, which is what makes reprocessing the
    #: corpus a no-op (M1 gate) instead of a deduplication problem solved
    #: separately in every loader.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Path inside the archive root, never absolute -- the root differs between
    #: environments and an absolute path would not survive the move.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=str(RawFileStatus.ARCHIVED),
        server_default="ARCHIVED",
    )
    #: DR-10: a quarantined file names the assertion it failed. Both columns
    #: are populated together or not at all -- enforced by the CHECK below,
    #: because "quarantined, reason unknown" is not an operator-actionable
    #: state and is how a quarantine queue becomes a place files go to die.
    quarantine_assertion: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Structured detail for the alert: amounts, claim ids, the failing line.
    quarantine_context: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)

    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_raw_file_content_hash"),
        CheckConstraint(
            "status IN ('ARCHIVED', 'LOADED', 'QUARANTINED')",
            name="status_is_known",
        ),
        CheckConstraint(
            "(status <> 'QUARANTINED') OR "
            "(quarantine_assertion IS NOT NULL AND quarantine_reason IS NOT NULL)",
            name="quarantine_names_its_assertion",
        ),
        Index("ix_raw_file_client_received", "client_id", "received_at"),
        Index("ix_raw_file_status", "status"),
    )


class Remittance(Base):
    """One 835 transaction set (loop header)."""

    __tablename__ = "remittance"

    remittance_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    raw_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("raw_file.raw_file_id", ondelete="RESTRICT"), nullable=False
    )

    #: PD-8: the payer is referenced by id. There is no payer_name column here
    #: acting as a key; the name lives on `payer`.
    payer_id: Mapped[str] = mapped_column(
        ForeignKey("payer.payer_id", ondelete="RESTRICT"), nullable=False
    )

    #: BPR02: the total of the payment this file accompanies.
    payment_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: TRN02: what ties this file to the deposit on the bank statement. The
    #: single most useful field when a practice asks where their money is.
    trace_number: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    transaction_control_number: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    payee_npi: Mapped[str] = mapped_column(
        String(20), nullable=False, default="", server_default=""
    )

    #: The file's own reconciliation, recorded rather than recomputed. A later
    #: reader must be able to see what the file claimed without re-deriving it
    #: from rows that may since have been corrected.
    claim_payment_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    provider_adjustment_total: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # One transaction set per control number per file. Re-running a load
        # cannot create a second copy.
        UniqueConstraint(
            "raw_file_id",
            "transaction_control_number",
            name="uq_remittance_raw_file_id_transaction_control_number",
        ),
        Index("ix_remittance_client_payer", "client_id", "payer_id"),
        Index("ix_remittance_payment_date", "payment_date"),
    )


class ClaimPayment(Base):
    """Loop 2100: what the payer did with one claim."""

    __tablename__ = "claim_payment"

    claim_payment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    remittance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("remittance.remittance_id", ondelete="CASCADE"), nullable=False
    )

    #: CLP01, as the provider submitted it. Not unique across the table: the
    #: same claim is adjudicated more than once when it is resubmitted, and
    #: forcing uniqueness here would make PD-7's chaining impossible to record.
    claim_id: Mapped[str] = mapped_column(String(80), nullable=False)
    #: CLP07: the payer's own control number. The handle needed to ask the
    #: payer about this claim.
    payer_control_number: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    #: CLP02. 1 processed as primary, 4 denied, 22 reversal of a prior payment.
    status_code: Mapped[str] = mapped_column(String(8), nullable=False)
    filing_indicator: Mapped[str] = mapped_column(
        String(8), nullable=False, default="", server_default=""
    )

    total_charge: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    total_paid: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    patient_responsibility: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)

    service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    member_id: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    rendering_provider_npi: Mapped[str] = mapped_column(
        String(20), nullable=False, default="", server_default=""
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_claim_payment_client_claim", "client_id", "claim_id"),
        Index("ix_claim_payment_remittance", "remittance_id"),
        Index("ix_claim_payment_payer_control", "payer_control_number"),
    )


class RemittanceServiceLine(Base):
    """Loop 2110: one adjudicated service line.

    Named for the loop rather than "service line" alone, because a submitted
    line (from an 837) and an adjudicated line are not the same thing and will
    both exist once M1.5 lands. A payer can bundle, split or re-code, so the
    procedure here is what was adjudicated, not necessarily what was sent.
    """

    __tablename__ = "remittance_service_line"

    service_line_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claim_payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claim_payment.claim_payment_id", ondelete="CASCADE"), nullable=False
    )

    #: Position within the claim, 1-based. Preserved because an 835 has no line
    #: identifier of its own and order is the only way back to the submitted
    #: line it corresponds to.
    line_number: Mapped[int] = mapped_column(nullable=False)
    procedure_code: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Up to four, stored as sent. A separate table would be tidier and would
    #: make the common query a join for data that is never queried
    #: independently of its line.
    modifiers: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )

    charge_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    #: AMT*B6. Nullable because some payers omit it, and a zero would be a
    #: different and wrong statement.
    allowed_amount: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)
    units: Mapped[Decimal] = mapped_column(
        _QUANTITY, nullable=False, default=Decimal("1"), server_default="1"
    )

    service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: LQ*HE remark codes qualifying the reason codes.
    remark_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "claim_payment_id", "line_number", name="uq_remittance_service_line_claim_line"
        ),
        Index("ix_remittance_service_line_client_procedure", "client_id", "procedure_code"),
    )


class RemittanceAdjustment(Base):
    """One CAS reason. **One row per reason** -- this table is PD-6.

    A line with three reasons is three rows here. The analytical grain in
    SRS 5.5 is one row per service line per adjustment reason, and it starts
    being true at this table or it is never true anywhere.
    """

    __tablename__ = "remittance_adjustment"

    adjustment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claim_payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claim_payment.claim_payment_id", ondelete="CASCADE"), nullable=False
    )
    #: Null for a claim-level CAS. Claim-level and line-level adjustments are
    #: different money and conflating them double-counts it.
    service_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("remittance_service_line.service_line_id", ondelete="CASCADE"),
        nullable=True,
    )

    #: PD-3. NOT NULL, and constrained to the five valid values. CO-45 and
    #: PR-45 are opposite outcomes and this column is the only difference.
    cas_group_code: Mapped[str] = mapped_column(String(2), nullable=False)
    #: CARC. The reason itself, as the payer sent it -- not mapped to an
    #: internal taxonomy here, because the mapping changes and the file does
    #: not.
    reason_code: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(_QUANTITY, nullable=True)
    claim_level: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("false")
    )

    #: Position within its CAS segment, preserved so the file can be rebuilt
    #: and so a reader can tell a genuine repeat from a loader bug.
    sequence: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "cas_group_code IN ('CO', 'PR', 'OA', 'PI', 'CR')",
            name="cas_group_code_is_valid",
        ),
        CheckConstraint("length(reason_code) > 0", name="reason_code_is_present"),
        # A claim-level adjustment has no line; a line-level one must have one.
        # Without this the two become indistinguishable through a loader bug
        # and the money is counted twice.
        CheckConstraint(
            "(claim_level AND service_line_id IS NULL) OR "
            "(NOT claim_level AND service_line_id IS NOT NULL)",
            name="claim_level_adjustments_have_no_service_line",
        ),
        Index("ix_remittance_adjustment_line", "service_line_id"),
        Index("ix_remittance_adjustment_claim", "claim_payment_id"),
        # The query behind every denial report: group and reason together,
        # because neither means anything alone.
        Index(
            "ix_remittance_adjustment_client_group_reason",
            "client_id",
            "cas_group_code",
            "reason_code",
        ),
    )


class ProviderAdjustment(Base):
    """A PLB entry: money moved outside any single claim.

    Recoupments, interest, forwarding balances. Ignoring these is what makes a
    file appear not to balance, so they are first-class rows rather than a
    footnote on the remittance.

    Sign convention is the standard's and is counter-intuitive: a **positive**
    amount *reduces* the payment.
    """

    __tablename__ = "provider_adjustment"

    provider_adjustment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    remittance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("remittance.remittance_id", ondelete="CASCADE"), nullable=False
    )

    provider_identifier: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    fiscal_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason_code: Mapped[str] = mapped_column(
        String(16), nullable=False, default="", server_default=""
    )
    #: Usually the payer control number of the claim being clawed back, which
    #: is what M1.10 needs to reopen the original.
    reference: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_provider_adjustment_remittance", "remittance_id"),
        Index("ix_provider_adjustment_reference", "reference"),
    )
