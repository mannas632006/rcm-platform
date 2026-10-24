"""CORE claim tables, with the resubmission chain (M1.9).

One business rule shapes this whole module:

    BR-DATA-03 (MUST): Resubmissions MUST be chained to the original claim,
    not stored as independent claims. Failure to do this inflates volume and
    distorts the denial rate.

It is worth being concrete about the size of that distortion, because it is
easy to read as a tidiness rule. On the synthetic corpus, 616 submitted claims
are 600 actual claims, and 107 of those submissions carry a denial. Counted per
submission the denial rate is 17.37%. Counted per claim -- which is what a
denial rate means, since resubmitting a denied claim does not deny it a second
time -- it is 15.17%. The wrong number is 14% higher than the right one, is
internally consistent, and is wrong in the direction that makes the business
look worse than it is. Nothing downstream can detect it.

**The chain is stored twice, on purpose.** `original_claim_id` is the edge
BR-DATA-03 and the SRS both describe: this claim replaces that one.
`root_claim_id` is that chain already resolved, so counting distinct claims is
`COUNT(DISTINCT root_claim_id)` rather than a recursive CTE. The redundancy
buys the thing that matters: a query that forgets the chain does not silently
return submission counts. It returns the same answer as a query that remembers.
That is the same trade already made for `client_id` under PD-20 -- a correctness
property should not depend on every future author remembering to write a join.

**PD-8, payer identity.** `payer_id` is a foreign key to `payer`. The payer's
name is on the payer row and is never a key here.

**DR-05, modifier order is semantically significant.** `modifier_1` through
`modifier_4` are ordered positional columns rather than an array or a set,
because a pricing modifier and an informational one are not interchangeable and
a set loses which was which.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base

#: Money: exact, never float.
_MONEY = Numeric(12, 2)

#: Units can be fractional (anaesthesia time, drug quantities), so SV104 is a
#: quantity rather than a count. Reading it as an integer bills 1.5 units of a
#: drug as 1.
_QUANTITY = Numeric(12, 3)

#: X12 permits four modifiers on a professional service line.
MAX_MODIFIERS = 4


class Claim(Base):
    """One submitted professional claim (837P).

    A row here is one *submission*. The claim it belongs to is
    `root_claim_id`, which is the row itself for an original claim and the
    first submission in the chain for every resubmission.
    """

    __tablename__ = "claim"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: PD-20: on every table, so `scoped_query` never depends on a join.
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    #: PD-9/PD-10: the archived file this was parsed from, so the row is
    #: rebuildable from bytes the platform still holds.
    raw_file_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("raw_file.raw_file_id"), nullable=False
    )

    #: CLM01, the submitter's own claim control number. Unique per client, which
    #: is what makes reloading the same file a no-op rather than a duplicate.
    submitter_claim_id: Mapped[str] = mapped_column(String(50), nullable=False)

    #: BR-DATA-03 / PD-7: the claim this one replaces, from REF*F8.
    original_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim.claim_id", ondelete="RESTRICT"), nullable=True
    )
    #: The chain resolved to its first submission. Equal to `claim_id` for an
    #: original claim. See the module docstring for why this is stored.
    root_claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim.claim_id", ondelete="RESTRICT"), nullable=False
    )
    #: 1 for an original claim, 2 for its first resubmission, and so on.
    submission_attempt: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    #: CLM05-3. 1 original, 7 replacement, 8 void (BR-M8-06). Recorded rather
    #: than trusted: senders set 7 with no REF*F8 and send REF*F8 with 1, so
    #: the reference is what chains and this is what the sender claimed.
    claim_frequency_code: Mapped[str] = mapped_column(
        String(2), nullable=False, default="1", server_default="1"
    )

    #: PD-8: identity on the payer's identifier, never a free-text name.
    payer_id: Mapped[str] = mapped_column(String(80), ForeignKey("payer.payer_id"), nullable=False)
    #: SBR01: P primary, S secondary, T tertiary.
    payer_responsibility: Mapped[str] = mapped_column(
        String(2), nullable=False, default="P", server_default="P"
    )
    #: SBR09, the claim filing indicator.
    claim_filing_code: Mapped[str] = mapped_column(
        String(4), nullable=False, default="", server_default=""
    )

    billing_npi: Mapped[str] = mapped_column(String(20), nullable=False)
    billing_tax_id: Mapped[str] = mapped_column(
        String(20), nullable=False, default="", server_default=""
    )
    #: The subscriber's member identifier as submitted. Not a patient key:
    #: patient and practice entities arrive with M3, and the foreign keys the
    #: SRS shows are an expand-migrate step once those tables exist.
    subscriber_member_id: Mapped[str] = mapped_column(String(80), nullable=False)
    practice_identifier: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )

    place_of_service: Mapped[str] = mapped_column(
        String(4), nullable=False, default="", server_default=""
    )
    #: BHT04, the date the submitter stated it created the transaction. This
    #: is the *business* submission date, and it is not the same thing as
    #: `raw_file.received_at`, which is when this platform archived the file.
    #:
    #: The difference only matters when they diverge, and they diverge exactly
    #: when it is most expensive: replaying a year of history in an afternoon
    #: gives every claim the same `received_at`. Any trailing-window feature
    #: computed as-of that timestamp then has no history behind it and comes
    #: back empty, which is DR-07 failing silently rather than loudly.
    #:
    #: Nullable because a file may omit BHT04; the loader falls back to the
    #: archival date and the mart records which it used.
    submitted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_of_service: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_charge: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)

    prior_auth_number: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", server_default=""
    )
    referral_number: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", server_default=""
    )

    #: Ordered: the principal diagnosis is first and every service line's
    #: diagnosis pointers are one-based indexes into this array. Sorting or
    #: de-duplicating it repoints every line on the claim.
    diagnoses: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default="{}"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Reloading the same claim is recognised, not duplicated (PD-14).
        UniqueConstraint("client_id", "submitter_claim_id", name="uq_claim_client_submitter_id"),
        # A claim cannot replace itself. Without this a single bad REF*F8 makes
        # a one-row cycle that every chain walk follows forever.
        CheckConstraint("original_claim_id <> claim_id", name="claim_does_not_replace_itself"),
        # The two representations of the chain must agree. An original claim is
        # its own root; a resubmission's root is some earlier claim. If these
        # can drift, the stored root stops being trustworthy and the whole
        # reason for storing it disappears.
        CheckConstraint(
            "(original_claim_id IS NULL AND root_claim_id = claim_id) OR "
            "(original_claim_id IS NOT NULL AND root_claim_id <> claim_id)",
            name="root_agrees_with_the_chain",
        ),
        # And the attempt number must agree with both (BR-DATA-03).
        CheckConstraint(
            "(original_claim_id IS NULL AND submission_attempt = 1) OR "
            "(original_claim_id IS NOT NULL AND submission_attempt > 1)",
            name="attempt_agrees_with_the_chain",
        ),
        CheckConstraint("total_charge >= 0", name="total_charge_is_not_negative"),
        CheckConstraint("length(submitter_claim_id) > 0", name="submitter_claim_id_is_present"),
        # The counting index. Every "how many claims" question groups by this.
        Index("ix_claim_root", "client_id", "root_claim_id"),
        Index("ix_claim_payer", "client_id", "payer_id"),
        # Every trailing-window feature orders by the submission date within
        # a client, and M2.5's temporal split cuts on it.
        Index("ix_claim_submitted_on", "client_id", "submitted_on"),
    )


class ClaimLine(Base):
    """One billed service on a claim (loop 2400).

    Adjudication happens per line (DOC-02 section 2.1), so this is the grain
    everything analytical is built on.
    """

    __tablename__ = "claim_line"

    claim_line_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim.claim_id", ondelete="RESTRICT"), nullable=False
    )

    line_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    procedure_code: Mapped[str] = mapped_column(String(16), nullable=False)

    #: DR-05: ordered positional columns, not an array and not a set. Modifier
    #: order is semantically significant -- pricing modifiers precede
    #: informational ones -- and a set loses which was which.
    modifier_1: Mapped[str | None] = mapped_column(String(2), nullable=True)
    modifier_2: Mapped[str | None] = mapped_column(String(2), nullable=True)
    modifier_3: Mapped[str | None] = mapped_column(String(2), nullable=True)
    modifier_4: Mapped[str | None] = mapped_column(String(2), nullable=True)

    units: Mapped[Decimal] = mapped_column(
        _QUANTITY, nullable=False, default=Decimal("1"), server_default="1"
    )
    charge_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    #: SV107, one-based indexes into the claim's `diagnoses` array.
    #: BR-M6-01 requires at least one, which the loader asserts before any row
    #: reaches this table.
    diagnosis_pointers: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, default=list, server_default="{}"
    )
    service_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("claim_id", "line_number", name="uq_claim_line_number"),
        CheckConstraint("line_number > 0", name="line_number_is_positive"),
        CheckConstraint("charge_amount >= 0", name="charge_amount_is_not_negative"),
        CheckConstraint("units > 0", name="units_are_positive"),
        CheckConstraint("length(procedure_code) > 0", name="procedure_code_is_present"),
        # BR-M6-01: every service line carries at least one diagnosis pointer.
        # Enforced here as well as in the loader, because a future loader that
        # forgets is exactly what a constraint is for.
        # `cardinality`, not `array_length`. On an empty array
        # `array_length(x, 1)` returns NULL rather than 0, a CHECK that
        # evaluates to NULL passes, and the constraint silently permits
        # exactly the row it exists to forbid. The security test caught this
        # by asserting the refusal rather than assuming it.
        CheckConstraint(
            "cardinality(diagnosis_pointers) >= 1", name="line_has_a_diagnosis_pointer"
        ),
        Index("ix_claim_line_claim", "client_id", "claim_id"),
    )
