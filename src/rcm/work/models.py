"""Work items: the queue a person actually works (M3.3, M3.4).

    FR-WORK-01 (MUST): Assignment places an exclusive lock. Two operators
    must never work one item (UR-04).

    FR-WORK-02 (MUST): Assignment locks expire after `[TBD-57]` minutes of
    inactivity and return the item to the queue. Operators go home
    mid-item.

The exclusivity is structural rather than procedural. `assigned_to` is one
column: an item has one holder or none, and the database cannot represent
two. What the service layer adds is the guarantee that two simultaneous
claims do not *both* see "none" -- which is a row lock, not a flag.

**Why the lock expires rather than being released.** An operator who goes
home mid-item does not release anything; a crashed browser releases nothing
either. A lock that is only given up deliberately is a lock that
accumulates, and the queue slowly fills with items nobody is working and
nobody can take. So holding is a lease with an end, and renewing it is what
working on something means.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

# The foreign key below targets `claim`, and a foreign key can only be built
# once its target is on the shared metadata. Importing the model here is what
# puts it there: without this, loading `rcm.work.models` alone raises
# `NoReferencedTableError`, which is a confusing way to be told that an
# import is missing. `rcm.work` sits above `rcm.domain` in the layer
# contract, so this direction is the permitted one.
from rcm.domain.claim import Claim as _Claim  # noqa: F401 - registers the FK target
from rcm.platform.db import Base

#: [TBD-57] How long an assignment lock survives without activity.
#:
#: FR-WORK-02 leaves the number to the sponsor and it is a real trade: too
#: short and an operator reading a chart loses the item mid-thought; too
#: long and an item sits locked for an afternoon after somebody went home.
#: Thirty minutes is a placeholder chosen to be obviously provisional, not
#: a measurement of how long this work takes.
DEFAULT_LOCK_MINUTES: Final = 30


class WorkItemKind(StrEnum):
    """What the item is about.

    Kinds exist so the queue can be split by skill -- a coder and an AR
    follow-up specialist are not interchangeable -- and so FR-EXC's volume
    monitoring has something to group by.
    """

    SCRUB_EXCEPTION = "SCRUB_EXCEPTION"
    SUBMISSION_REJECTION = "SUBMISSION_REJECTION"
    DENIAL = "DENIAL"
    PAYMENT_EXCEPTION = "PAYMENT_EXCEPTION"
    ELIGIBILITY_PROBLEM = "ELIGIBILITY_PROBLEM"
    AUTH_PROBLEM = "AUTH_PROBLEM"
    PAYER_MATCH_REVIEW = "PAYER_MATCH_REVIEW"
    DATA_QUALITY = "DATA_QUALITY"


class WorkItem(Base):
    """One piece of work, for one person, at one time."""

    __tablename__ = "work_item"

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: PD-20: on every table, so scoping never depends on a join.
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    #: Most items are about a claim. Some are not -- a payer match awaiting
    #: review belongs to the registry, not to any one claim -- so this is
    #: nullable rather than forced.
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim.claim_id"), nullable=True
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="Created", server_default="Created"
    )

    #: BR-EXC-01: every exception names the specific problem and the
    #: specific record. A generic title is a defect, and the constraint
    #: below refuses an empty one.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    #: 0-100. 100 is reserved for deadline-pinned items (FR-WORK-05).
    priority_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: PD-15/FR-WORK-05. Set when the filing deadline is close enough that
    #: no score may demote the item. Stored rather than recomputed so the
    #: queue can be read without re-deriving every claim's deadline.
    deadline_pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: FR-WORK-01. One holder or none; the column cannot represent two.
    assigned_to: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: FR-WORK-02. When the lease ends. Null whenever nobody holds the item.
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: FR-WORK-04: structured, not free text. Null until resolved.
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    #: BR-EXC-02: every exception has an owner and an SLA.
    owner_role: Mapped[str] = mapped_column(String(48), nullable=False)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('Created','Queued','Assigned','InProgress','Escalated',"
            "'Resolved','Reopened','AutoResolved')",
            name="work_item_state_is_known",
        ),
        # FR-WORK-01, as a schema fact rather than a convention. A held item
        # names its holder and carries a lease; an unheld one does neither.
        # Half of either pair is a lock nobody can see or a holder nobody
        # can time out.
        CheckConstraint(
            "(state IN ('Assigned','InProgress')) = "
            "(assigned_to IS NOT NULL AND lock_expires_at IS NOT NULL)",
            name="a_held_item_names_its_holder_and_its_lease",
        ),
        # FR-WORK-04. A resolved item has a structured outcome; an
        # unresolved one has none. "Resolved, reason unknown" is the row
        # this forbids, and it is the row that makes the training signal
        # useless.
        CheckConstraint(
            "(state IN ('Resolved','AutoResolved')) = (outcome IS NOT NULL)",
            name="a_resolved_item_records_its_outcome",
        ),
        # BR-EXC-01: name the specific problem.
        CheckConstraint("length(title) > 0", name="work_item_names_its_problem"),
        # BR-EXC-02: an owner, always.
        CheckConstraint("length(owner_role) > 0", name="work_item_names_its_owner"),
        CheckConstraint("priority_score BETWEEN 0 AND 100", name="priority_score_is_a_percentage"),
        # The queue read: open items for one client, best first.
        Index("ix_work_item_queue", "client_id", "state", "priority_score"),
        # The expiry sweep (FR-WORK-02).
        Index("ix_work_item_lease", "client_id", "lock_expires_at"),
        Index("ix_work_item_claim", "client_id", "claim_id"),
    )
