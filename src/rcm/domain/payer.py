"""Payer identity (PD-8).

    PD-8  Payer identity keys on `payer_id`, never free-text name.

The directive exists because payer names are not identifiers. The same
organisation arrives as "BCBS TX", "Blue Cross Texas", "BLUE CROSS BLUE SHIELD
OF TEXAS" and "BCBSTX" across four clearinghouses and two decades of files.
Keying on the name produces four payers, four denial rates, four sets of rules,
and a payer registry that is wrong in a way nobody notices because each of the
four looks internally consistent.

So `payer_id` is the primary key and `name` carries no uniqueness constraint at
all. The alias table is where the many names live, and M1.7 adds the matching
that populates it; this is the shape it needs to exist in first.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base


class Payer(Base):
    """An insurer, identified the way X12 identifies one.

    `payer_id` is the identifier that appears in the file -- NM1*PR or the
    CLP's payer control context -- not a surrogate key. A surrogate would be
    tidier and would put a lookup between every claim and the payer it names,
    which is one more place for the identity to drift.
    """

    __tablename__ = "payer"

    #: The payer's own identifier, as it appears in X12. PD-8's key.
    payer_id: Mapped[str] = mapped_column(String(80), primary_key=True)

    #: Display name. Deliberately **not** unique and deliberately not a key.
    #: A unique constraint here would be PD-8 violated in the schema.
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Commercial, Medicare, Medicaid, Medicare Advantage, Workers Comp.
    #: Drives timely-filing and appeal rules, so it is kept on the payer rather
    #: than re-derived per claim.
    plan_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Days from date of service to the filing deadline. Null until the payer
    #: registry (M4.10) has verified it; a null here must not be read as "no
    #: deadline", which is why the column is nullable rather than defaulted.
    timely_filing_days: Mapped[int | None] = mapped_column(nullable=True)

    #: BR-REG-02: a registry entry too old to drive a BLOCKING decision. The
    #: staleness rule lives in M4.10; the date it needs lives here.
    last_verified_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PayerAlias(Base):
    """One of the many names a payer arrives under.

    Populated by the normalisation in M1.7. The `confirmed` flag separates an
    alias a human accepted from one fuzzy matching proposed: an unconfirmed
    alias is a suggestion, and resolving a claim through one without review is
    how two payers quietly become one.
    """

    __tablename__ = "payer_alias"

    alias_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payer_id: Mapped[str] = mapped_column(
        ForeignKey("payer.payer_id", ondelete="RESTRICT"), nullable=False
    )
    #: The raw text seen in a file, normalised for comparison but not for
    #: display -- the original spelling is evidence of where it came from.
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalised_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # One normalised spelling resolves to one payer. Without this, the same
        # text could map to two payers and resolution would depend on row order.
        UniqueConstraint("normalised_alias", name="uq_payer_alias_normalised_alias"),
        Index("ix_payer_alias_payer_id", "payer_id"),
    )


class MatchCandidateStatus(StrEnum):
    """Where a proposed duplicate sits in review."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class PayerMatchCandidate(Base):
    """Two payer ids that look like the same payer, awaiting a human (M1.7).

    This is a *proposal*, never an action. Merging two payers changes identity,
    and identity is the one thing PD-8 puts out of reach of a heuristic -- so
    nothing in the platform acts on a row here until its status is CONFIRMED,
    and even then the acting is someone else's job.

    The case this was built for is already in the corpus: an 837 names the
    payer `00590` and the 835 answering it names `PAYER00590`, because a
    clearinghouse ETIN and a payer ID are different identifiers from different
    namespaces. Sixteen payer rows for eight payers, each with its own denial
    rate.
    """

    __tablename__ = "payer_match_candidate"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: Ordered so that the same pair is always stored the same way round,
    #: which is what makes the uniqueness constraint below mean "this pair has
    #: already been proposed" rather than "this direction has".
    payer_id: Mapped[str] = mapped_column(
        ForeignKey("payer.payer_id", ondelete="RESTRICT"), nullable=False
    )
    other_payer_id: Mapped[str] = mapped_column(
        ForeignKey("payer.payer_id", ondelete="RESTRICT"), nullable=False
    )
    #: What the match was made on, kept so a reviewer can see the evidence
    #: rather than a score alone.
    matched_on: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=str(MatchCandidateStatus.PENDING),
        server_default="PENDING",
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("payer_id", "other_payer_id", name="uq_payer_match_candidate_pair"),
        # A payer is not a candidate duplicate of itself.
        CheckConstraint("payer_id <> other_payer_id", name="candidate_pairs_two_payers"),
        # Stored one way round only, so the pair uniqueness above holds.
        CheckConstraint("payer_id < other_payer_id", name="candidate_pair_is_ordered"),
        CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'REJECTED')", name="candidate_status_is_known"
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="candidate_confidence_is_a_ratio"
        ),
        # A reviewed candidate records who and when; an unreviewed one records
        # neither. "Confirmed by nobody at no time" is not an audit trail.
        CheckConstraint(
            "(status = 'PENDING' AND reviewed_at IS NULL AND reviewed_by IS NULL) OR "
            "(status <> 'PENDING' AND reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)",
            name="a_reviewed_candidate_names_its_reviewer",
        ),
        Index("ix_payer_match_candidate_status", "status"),
    )
