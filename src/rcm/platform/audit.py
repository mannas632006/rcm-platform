"""Append-only audit and decision records (PD-1, PD-2, DR-06, SR-01, BR-AUDIT-01).

These two tables are how the platform answers "why did this happen" a year later.
The SRS section 5.4 calls them "not optional infrastructure", and they are the reason
CLAUDE.md section 0 can claim every automated decision is explainable.

**These tables hold PHI, and that is correct.** The scrubber in
`rcm.platform.logging` exists because logs, metrics, traces and error payloads
leave the trust boundary. An audit row does not: it lives in the encrypted
database behind the same access controls as the claim it describes. Scrubbing
`before_state` would destroy the only record of what actually changed, which is
the thing HIPAA access logging exists to preserve. Do not "fix" this by scrubbing
here.

**Append-only is enforced by database grants**, not by this module. The
application role holds no UPDATE or DELETE on either table (see the migration that
revokes them). The ORM-level guard below is defence in depth -- it turns a
programming mistake into a clear error at the call site instead of a permission
error from the driver -- but it is not the control. A guard the application can
edit is not a control against the application.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    String,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from rcm.platform.db import Base

#: Tables no code path may update or delete.
APPEND_ONLY_TABLES: Final[frozenset[str]] = frozenset({"audit_event", "decision_record"})


class ActorType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"


class DecisionType(StrEnum):
    """Decision kinds recorded under PD-1.

    Extended as milestones add decision points; every value here corresponds to a
    place the system chooses something a human could be asked to justify.
    """

    SCRUB = "SCRUB"
    CODE = "CODE"
    PRIORITISE = "PRIORITISE"
    CLASSIFY_DENIAL = "CLASSIFY_DENIAL"
    ELIGIBILITY = "ELIGIBILITY"
    SUBMIT = "SUBMIT"
    APPEAL = "APPEAL"
    PAYER_MATCH = "PAYER_MATCH"


class DecisionRecord(Base):
    """One automated decision: its inputs, the logic version, and its output.

    PD-1 requires all three. `decided_by` distinguishes `rules_engine` from
    `model:<name>` from `user:<id>`, because "the model decided" is exactly the
    answer CLAUDE.md section 0 rules out.
    """

    __tablename__ = "decision_record"

    decision_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Ruleset version or model version. Not nullable: a decision whose logic
    #: version is unknown cannot be reproduced, which makes it unexplainable.
    logic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    #: Required for coding decisions (FR-ML-09): no citation, no code.
    citations: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_decision_record_entity_decided_at",
            "entity_type",
            "entity_id",
            decided_at.desc(),
        ),
    )


class AuditEvent(Base):
    """One access to, or change of, PHI.

    SR-01: every PHI read produces one of these. It is a HIPAA access-logging
    requirement, not a debugging nicety -- which is why reads are logged, not
    only writes.
    """

    __tablename__ = "audit_event"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_audit_event_entity_occurred_at", "entity_type", "entity_id", occurred_at.desc()),
        Index("ix_audit_event_actor_occurred_at", "actor_id", occurred_at.desc()),
    )


class AppendOnlyViolationError(RuntimeError):
    """Raised when code attempts to modify an append-only row.

    A programming error, surfaced at the call site. The database refuses the same
    operation independently; this exists so the failure names the rule.
    """


@event.listens_for(Session, "before_flush")
def _forbid_audit_mutation(session: Session, _flush_context: object, _instances: object) -> None:
    """Defence in depth for DR-06.

    The grant is the control. This makes a mistake legible.
    """
    for instance in list(session.dirty) + list(session.deleted):
        table = getattr(type(instance), "__tablename__", None)
        if table in APPEND_ONLY_TABLES:
            operation = "update" if instance in session.dirty else "delete"
            raise AppendOnlyViolationError(f"{table} is append-only (DR-06); attempted {operation}")


def record_decision(
    session: Session,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    decision_type: DecisionType,
    decided_by: str,
    logic_version: str,
    input_snapshot: dict[str, Any],
    output: dict[str, Any],
    confidence: float | None = None,
    citations: dict[str, Any] | None = None,
) -> DecisionRecord:
    """Write a decision record into the caller's transaction (PD-1).

    Takes the caller's `session` deliberately. A decision recorded in its own
    transaction could survive a rollback of the decision it describes, leaving an
    audit trail for something that never happened.
    """
    record = DecisionRecord(
        entity_type=entity_type,
        entity_id=entity_id,
        decision_type=str(decision_type),
        decided_by=decided_by,
        logic_version=logic_version,
        input_snapshot=input_snapshot,
        output=output,
        confidence=confidence,
        citations=citations,
    )
    session.add(record)
    return record


def record_audit_event(
    session: Session,
    *,
    actor_type: ActorType,
    actor_id: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    ip_address: str | None = None,
    session_id: uuid.UUID | None = None,
) -> AuditEvent:
    """Write an audit event into the caller's transaction (PD-2).

    Same reasoning as `record_decision`, and PD-2 states it outright: the audit
    event is written *in the same transaction as the change*. If the change rolls
    back, so does its audit row -- otherwise the log accumulates changes that
    never happened, and every later reconciliation is wrong.
    """
    audit = AuditEvent(
        actor_type=str(actor_type),
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=before_state,
        after_state=after_state,
        ip_address=ip_address,
        session_id=session_id,
    )
    session.add(audit)
    return audit


def utcnow() -> datetime:
    """Timezone-aware now. Naive datetimes are banned by the DTZ lint rules."""
    return datetime.now(UTC)
