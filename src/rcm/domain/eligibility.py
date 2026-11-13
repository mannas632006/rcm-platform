"""What a payer said about a member's coverage: stored 271 responses
(DOC-02 M3; ADR-0034).

The 271 was parsed since M1.6 and not kept. It is kept now because two
decisions need it later than the moment it arrives:

- the scrub asks whether the patient was covered on the date of service
  (EC-13, EC-15);
- the denial feedback loop asks whether a denial contradicts the payer's
  own earlier answer (EC-61).

**What is kept, and what is not.** The payer, the member identifier and
every benefit with its coverage periods: the facts the checks read. The
subscriber's name and date of birth are not kept. They are in the archived
file (PD-9), and no check here needs them, so they are not copied into
another table (PD-13: PHI goes no further than it must).

**A refused enquiry is not a negative answer.** `answered` is false when the
payer only said it could not look (AAA). Nothing reads that as "not
covered" (PD-18).

Insert-only by grant (migration 0032): what a payer said is evidence.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base


class EligibilityResponseRecord(Base):
    """One 271 transaction: the payer's answer about one member."""

    __tablename__ = "eligibility_response"

    response_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    raw_file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("raw_file.raw_file_id"), nullable=False
    )
    transaction_control_number: Mapped[str] = mapped_column(String(32), nullable=False)
    #: NM1*PR 09: the payer, keyed as a claim keys it (PD-8).
    payer_id: Mapped[str] = mapped_column(String(80), nullable=False)
    #: NM1*IL 09: the member, matched to `claim.subscriber_member_id`.
    member_id: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Did the payer say anything about coverage at all (EB01 1-8, I, V)?
    answered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Every EB: code, coverage level, service type, insurance type, plan
    #: description and coverage periods. Plan terms, not patient details.
    benefits: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    #: Every AAA: why the payer could not answer.
    problems: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: BHT03, the enquiry's own reference, where the payer echoes it.
    reference: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_eligibility_member", "client_id", "payer_id", "member_id", "received_at"),
    )
