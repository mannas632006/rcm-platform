"""The record of how a claim got where it is (M3.2).

    BR-STATE-01 (MUST): Every state transition MUST be logged with
    timestamp, actor (human or system), and reason.

This is not `audit_event` and does not replace it. `audit_event` is the
HIPAA access record: append-only, owned by the platform, written for every
PHI read and every change, and read by compliance. This table is the claim's
own history, read by operators and by the denial analytics -- "how many
claims went Denied -> Appealing -> Paid last quarter" is a question about
these rows, and answering it from the audit log would mean parsing JSON
payloads written for a different purpose.

Both are written in the same transaction as the state change (PD-2,
FR-BE-12), so they cannot disagree.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base


class ClaimStateTransition(Base):
    """One move from one state to another."""

    __tablename__ = "claim_state_transition"

    transition_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: PD-20: on every table, so scoping never depends on a join.
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim.claim_id"), nullable=False
    )

    source_state: Mapped[str] = mapped_column(String(32), nullable=False)
    target_state: Mapped[str] = mapped_column(String(32), nullable=False)

    #: The domain's sentence for why this edge exists ("277CA accepted the
    #: claim"), copied from the transition table. The pair of state names is
    #: already in the two columns above; this is the part a human reads.
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Anything the actor added about this particular claim.
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: Which guard permitted it, where one applied. Kept because "why did
    #: this claim go to SecondaryBilling" is answered by the fact that
    #: secondary coverage existed, not by the edge.
    guard: Mapped[str] = mapped_column(String(48), nullable=False, default="", server_default="")

    #: BR-STATE-01: human or system, and which one.
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: BR-STATE-04: what was scheduled when the claim entered this state.
    #: Null for the states a claim rests in.
    next_action_due: Mapped[date | None] = mapped_column(Date, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # A transition from a state to itself is legitimate -- a 276/277
        # status check that changes nothing still happened and still resets
        # the next action date -- so there is deliberately no constraint
        # forbidding it. What is forbidden is a row that names no states.
        CheckConstraint("length(source_state) > 0", name="transition_names_its_source"),
        CheckConstraint("length(target_state) > 0", name="transition_names_its_target"),
        # BR-STATE-01. A transition with no reason is a row that records
        # that something happened and refuses to say what.
        CheckConstraint("length(reason) > 0", name="transition_states_a_reason"),
        CheckConstraint("length(actor_id) > 0", name="transition_names_its_actor"),
        Index("ix_claim_transition_claim", "client_id", "claim_id", "occurred_at"),
        Index("ix_claim_transition_target", "client_id", "target_state"),
    )
