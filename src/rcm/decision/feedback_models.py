"""What each classified denial produced in the feedback loop (M4.9, ADR-0013).

Insert-only by grant. One row per classified denial per taxonomy version:
FR-RULES-07's "a candidate rule or a recorded decision not to create one",
held as a fact that cannot be quietly rewritten.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.decision.denial_models import DenialClassification as _Classified  # noqa: F401 - FK
from rcm.domain.claim import Claim as _Claim  # noqa: F401 - FK target
from rcm.platform.db import Base

#: Spelled for the CHECK; migration 0023 spells them again, and
#: tests/unit/test_feedback_candidates.py fails if either drifts from
#: `rcm.decision.feedback.FeedbackOutcome`.
OUTCOMES = (
    "'ALREADY_COVERED', 'CANDIDATE_DRAFTED', 'CANDIDATE_EXISTS', 'LINE_UNMATCHED', "
    "'NOT_PREVENTABLE', 'NO_CLAIM', 'NO_RULE', 'RULE_FAILED'"
)


class DenialFeedback(Base):
    __tablename__ = "denial_feedback"

    service_line_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    taxonomy_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claim_payment_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: The submitted claim the denial was traced to; None when no 837 is on
    #: file for it (NO_CLAIM).
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim.claim_id"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    #: The candidate drafted or joined, the rule that failed, or the check
    #: that covers it now. Not a foreign key: an NCCI edit is a check, not a
    #: stored rule version.
    rule_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Why, in words a reviewer can act on. Codes and rule names only (PD-13).
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The classification it answers, with its client and posting (PD-20).
        ForeignKeyConstraint(
            ["service_line_id", "taxonomy_version", "client_id", "claim_payment_id"],
            [
                "denial_classification.service_line_id",
                "denial_classification.taxonomy_version",
                "denial_classification.client_id",
                "denial_classification.claim_payment_id",
            ],
            name="fk_denial_feedback_classification",
        ),
        CheckConstraint(f"outcome IN ({OUTCOMES})", name="outcome_is_known"),
        # A candidate, a failed rule or a covering check is named; the others
        # name none.
        CheckConstraint(
            "(outcome IN ('CANDIDATE_DRAFTED', 'CANDIDATE_EXISTS', 'RULE_FAILED', "
            "'ALREADY_COVERED')) = (rule_id IS NOT NULL)",
            name="names_its_rule",
        ),
        CheckConstraint("length(trim(reason)) > 0", name="states_a_reason"),
        Index("ix_denial_feedback_client_outcome", "client_id", "outcome"),
        Index("ix_denial_feedback_rule", "rule_id"),
    )
