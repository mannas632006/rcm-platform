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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
from rcm.work.state import WorkItemOutcome, WorkItemState


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
    # M3.5: the exception kinds DOC-02 section 10.1 routes.
    RULE_CONFLICT = "RULE_CONFLICT"
    MODEL_LOW_CONFIDENCE = "MODEL_LOW_CONFIDENCE"
    EXTERNAL_SYSTEM_FAILURE = "EXTERNAL_SYSTEM_FAILURE"
    COMPLIANCE_CONCERN = "COMPLIANCE_CONCERN"
    DEADLINE_RISK = "DEADLINE_RISK"
    #: A payer with no recorded timely-filing term. Asks the user for it;
    #: until it is given the claim is urgent (owner decision, 2026-11-01).
    MISSING_FILING_DEADLINE = "MISSING_FILING_DEADLINE"


class ExceptionCategory(StrEnum):
    """DOC-02 section 10.1's routing classes. The category decides the
    owner and the SLA; the kind says what specifically went wrong."""

    DATA_QUALITY = "DATA_QUALITY"
    RULE_CONFLICT = "RULE_CONFLICT"
    MODEL_LOW_CONFIDENCE = "MODEL_LOW_CONFIDENCE"
    EXTERNAL_SYSTEM = "EXTERNAL_SYSTEM"
    COMPLIANCE = "COMPLIANCE"
    DEADLINE_RISK = "DEADLINE_RISK"
    #: Ordinary RCM work -- denials, rejections, payment exceptions -- that
    #: is not an automation failure but still needs an owner and an SLA.
    OPERATIONAL = "OPERATIONAL"


class AutomationPath(StrEnum):
    """Automation a compliance exception can suspend (BR-EXC-03)."""

    CLAIM_PROGRESSION = "CLAIM_PROGRESSION"
    CLAIM_SUBMISSION = "CLAIM_SUBMISSION"
    AUTO_CORRECTION = "AUTO_CORRECTION"
    AUTO_RESOLUTION = "AUTO_RESOLUTION"
    REMITTANCE_POSTING = "REMITTANCE_POSTING"


def _one_of(column: str, values: type[StrEnum]) -> str:
    """`column IN (...)` over an enum, so the CHECK cannot drift from it.

    Migration 0011 writes the same lists out by hand (Alembic does not
    autogenerate CHECKs), and `tests/security/test_work_item_constraints.py`
    compares the live constraints with these enums.
    """
    quoted = ",".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


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
    #: BR-EXC-02 routing class; decides the owner and the SLA.
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    #: What fact raised this, e.g. `quarantine:<raw_file_id>`. Raising the
    #: same fact while an item for it is still open returns that item
    #: rather than a duplicate (see the partial unique index below).
    source_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
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
    #: FR-WORK-06: why the item ranks where it does, in words and numbers.
    priority_reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    priority_components: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    #: PD-1: the logic that produced the score. Null until first scored.
    priority_logic_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prioritised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Days left to file when last prioritised; orders pinned items by how
    #: close they are. Null when unknown or when the clock has stopped.
    days_to_deadline: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: BR-M10-06: expected recovery below [TBD-46] -- bulk, not individual work.
    bulk_handling: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: FR-WORK-01. One holder or none; the column cannot represent two.
    assigned_to: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: FR-WORK-02. When the lease ends. Null whenever nobody holds the item.
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: FR-WORK-03. When a human first took the item; never cleared. Released
    #: and reopened items go back to `Queued`, so the state alone cannot say
    #: whether a person ever worked it -- and an item a person worked must
    #: not be counted as automation.
    first_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: FR-WORK-04: structured, not free text. Null until resolved.
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    #: BR-EXC-02: every exception has an owner and an SLA.
    owner_role: Mapped[str] = mapped_column(String(48), nullable=False)
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

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
        # PD-15 / FR-WORK-05: 100 means pinned and only pinned. A pinned
        # item cannot carry a demotable score, and nothing scored can
        # quietly claim the top of the queue.
        CheckConstraint(
            "deadline_pinned = (priority_score = 100)", name="only_a_pinned_item_scores_100"
        ),
        # A pinned item is a person's work, never bulk.
        CheckConstraint(
            "NOT (deadline_pinned AND bulk_handling)", name="a_pinned_item_is_not_bulk"
        ),
        # FR-WORK-04 at the schema level: an outcome is one of the structured
        # values or nothing. Free text here is the training signal lost.
        CheckConstraint(
            f"outcome IS NULL OR {_one_of('outcome', WorkItemOutcome)}",
            name="work_item_outcome_is_known",
        ),
        CheckConstraint(_one_of("kind", WorkItemKind), name="work_item_kind_is_known"),
        CheckConstraint(_one_of("category", ExceptionCategory), name="work_item_category_is_known"),
        # Idempotent raising: one open item per fact. Partial, so a fact
        # that recurs after its item was resolved raises fresh work.
        Index(
            "uq_work_item_open_source",
            "client_id",
            "kind",
            "source_key",
            unique=True,
            postgresql_where=text(
                "source_key IS NOT NULL AND state IN "
                "('Created','Queued','Assigned','InProgress','Escalated','Reopened')"
            ),
        ),
        # FR-WORK-03: automation coverage counts only items no human held.
        CheckConstraint(
            f"state <> '{WorkItemState.AUTO_RESOLVED}' OR first_claimed_at IS NULL",
            name="auto_resolved_was_never_held",
        ),
        # The queue read: open items for one client, best first.
        Index("ix_work_item_queue", "client_id", "state", "priority_score"),
        # M3.6's read: open, individual work, pinned first, then by score.
        Index(
            "ix_work_item_next",
            "client_id",
            "state",
            "bulk_handling",
            "deadline_pinned",
            "priority_score",
        ),
        # The expiry sweep (FR-WORK-02).
        Index("ix_work_item_lease", "client_id", "lock_expires_at"),
        Index("ix_work_item_claim", "client_id", "claim_id"),
    )


class AutomationSuspension(Base):
    """An automation path stopped by a compliance exception (BR-EXC-03).

        FR-BE-07 (MUST): Compliance-class errors suspend the affected
        automation path pending human review.

    A row with `lifted_at IS NULL` is an active suspension. It is lifted
    only after the item that raised it is resolved, so "until reviewed"
    means a person looked, not that time passed.
    """

    __tablename__ = "automation_suspension"

    suspension_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    path: Mapped[str] = mapped_column(String(32), nullable=False)
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("work_item.work_item_id"), nullable=False
    )
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint(_one_of("path", AutomationPath), name="suspension_path_is_known"),
        # Lifting names who lifted it; an unlifted row names nobody.
        CheckConstraint(
            "(lifted_at IS NULL) = (lifted_by IS NULL) "
            "AND (lifted_by IS NULL OR length(lifted_by) > 0)",
            name="a_lift_names_who_lifted_it",
        ),
        Index("ix_suspension_active", "client_id", "path", "lifted_at"),
    )
