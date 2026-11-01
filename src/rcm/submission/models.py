"""The submission outbox table (M3.7, IR-06, IR-07).

One row per file to transmit. It is written in the same transaction that
moves the claim to `Submitted`, and a separate dispatcher sends it -- so the
database can never say "submitted" about a file that will not be sent, and
a crash between the two leaves a row to retry rather than a claim nobody
sends.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.domain.claim import Claim as _Claim  # noqa: F401 - registers the FK target
from rcm.platform.db import Base


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"


class SubmissionOutbox(Base):
    __tablename__ = "submission_outbox"

    outbox_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim.claim_id"), nullable=False
    )
    #: IR-07: the same key is the same submission, here and at the
    #: clearinghouse.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The 837 to send. PHI, held in the database like the rest of CORE and
    #: never logged.
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default="PENDING"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: An error *code* only -- never a message that could carry PHI.
    last_error: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_via: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_outbox_idempotency_key"),
        CheckConstraint("status IN ('PENDING','SENT')", name="outbox_status_is_known"),
        # A sent row says when, how and with what receipt; a pending one
        # says none of it.
        CheckConstraint(
            "(status = 'SENT') = "
            "(sent_at IS NOT NULL AND sent_via IS NOT NULL AND receipt_id IS NOT NULL)",
            name="a_sent_row_records_its_receipt",
        ),
        CheckConstraint("attempts >= 0", name="outbox_attempts_are_counted"),
        CheckConstraint("length(idempotency_key) > 0", name="outbox_has_a_key"),
        Index("ix_outbox_due", "client_id", "status", "next_attempt_at"),
    )
