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

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
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
