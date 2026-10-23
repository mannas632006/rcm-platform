"""Rejections, kept apart from denials by the schema (M1.6).

    BR-GLOBAL-02 (MUST): Rejections and denials MUST be stored and reported
    as distinct outcome types. Conflating them inflates the denial rate and
    hides front-end data quality problems.

PD-4 says the same thing in four words: rejections are not denials. They look
alike -- both are a claim not being paid -- and they behave oppositely:

    | reached adjudication? | rejection: no      | denial: yes       |
    | arrives via           | 999 / 277CA        | 835               |
    | timely filing         | clock keeps running| date preserved    |
    | appealable?           | no, fix and resend | yes               |
    | counts in denial rate | no                 | yes               |

The separation here is structural rather than a flag. Rejections land in this
table; denials are derived from `remittance_service_line` and
`remittance_adjustment`. A rejection therefore cannot reach the denial rate
however that query is written, because the rows are not in the tables it
reads. The same argument the remittance loader makes for never creating a
claim: a rule nobody has to remember is better than one everybody must.

**BR-M8-02**: *"A claim MUST NOT be considered submitted until a 277CA
acceptance is received. 999 acceptance alone is insufficient -- it confirms
syntax, not acceptance."* So `source` is recorded alongside the outcome, and
an acceptance from a 999 is not the same row as an acceptance from a 277CA.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base


class AcknowledgementSource(StrEnum):
    """Which transaction carried the verdict.

    The distinction is load-bearing, not bookkeeping: BR-M8-02 makes a 999
    acceptance insufficient for submission and a 277CA acceptance sufficient,
    so a query that cannot tell them apart cannot answer "is this claim
    submitted".
    """

    #: Functional acknowledgement: the transmission parsed. Syntax only.
    SYNTAX_999 = "999"
    #: Claim acknowledgement: the payer's front-end verdict.
    CLAIM_277CA = "277CA"
    #: Claim status response to an enquiry, later in the claim's life.
    STATUS_277 = "277"


class AcknowledgementOutcome(StrEnum):
    """What the verdict was.

    Two values and no third. There is deliberately no `DENIED` here -- a
    denial is not an acknowledgement outcome, it is what an 835 says about a
    claim that was adjudicated, and the absence of the word from this enum is
    the point.
    """

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ClaimAcknowledgement(Base):
    """One verdict on one claim, from a 999, a 277CA or a 277."""

    __tablename__ = "claim_acknowledgement"

    acknowledgement_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: PD-20: on every table, so scoping never depends on a join.
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: PD-9/PD-10: the archived file this came from.
    raw_file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("raw_file.raw_file_id"), nullable=False
    )

    #: The submitter's claim control number, as the acknowledgement names it.
    #: Text rather than a foreign key to `claim`: an acknowledgement can
    #: arrive for a claim this platform never loaded, and losing the record
    #: of a rejection because its claim is missing is the wrong trade.
    claim_id: Mapped[str] = mapped_column(String(80), nullable=False)
    #: REF*1K, the payer's own claim number where it gives one.
    payer_claim_number: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )

    source: Mapped[str] = mapped_column(String(8), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)

    #: STC01-1 and STC01-2 for a 277; the AK9/IK5 code for a 999. Kept so a
    #: reviewer sees why, not just that.
    category_code: Mapped[str] = mapped_column(
        String(8), nullable=False, default="", server_default=""
    )
    status_code: Mapped[str] = mapped_column(
        String(8), nullable=False, default="", server_default=""
    )
    #: STC12 free text, which is often the only human-readable reason.
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The same file saying the same thing about the same claim twice is
        # one fact. Re-presenting a file is recognised upstream by content
        # hash; this is the guard for a loader that gets past it.
        UniqueConstraint(
            "raw_file_id", "claim_id", "source", "category_code", name="uq_acknowledgement_fact"
        ),
        CheckConstraint("source IN ('999', '277CA', '277')", name="source_is_known"),
        # Two outcomes, and DENIED is not one of them (PD-4).
        CheckConstraint("outcome IN ('ACCEPTED', 'REJECTED')", name="outcome_is_known"),
        CheckConstraint("length(claim_id) > 0", name="acknowledgement_names_its_claim"),
        # A rejection that does not say why is not actionable, and the whole
        # value of a rejection over a denial is that it can be fixed and
        # resent. 999s carry their reason in the error detail rather than a
        # category, so they are exempt.
        CheckConstraint(
            "outcome <> 'REJECTED' OR source = '999' OR length(category_code) > 0",
            name="a_rejection_names_its_reason",
        ),
        Index("ix_acknowledgement_client_claim", "client_id", "claim_id"),
        Index("ix_acknowledgement_outcome", "client_id", "outcome"),
    )
