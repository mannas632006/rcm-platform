"""Denial classifications (M4.8, ADR-0012). Insert-only by grant."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.domain.remittance import RemittanceServiceLine as _Line  # noqa: F401 - FK target
from rcm.platform.db import Base

#: The ten DOC-02 categories, spelled for the CHECK. Migration 0021 spells
#: them again; tests/unit/test_denial_taxonomy.py fails if either drifts from
#: `rcm.decision.denials.DenialCategory`.
CATEGORIES = (
    "'AUTHORISATION', 'CODING', 'CONTRACTUAL_NON_COVERED', 'COORDINATION_OF_BENEFITS', "
    "'DUPLICATE', 'MEDICAL_NECESSITY', 'PAYER_ERROR', 'PROVIDER_ENROLMENT', "
    "'REGISTRATION_ELIGIBILITY', 'TIMELY_FILING'"
)
PREVENTABILITY = "'NOT_PREVENTABLE', 'PARTIALLY_PREVENTABLE', 'PREVENTABLE'"


class DenialClassification(Base):
    """One denied 835 service line, classified under one taxonomy version.

    BR-TAX-01: one category and one preventability, never more -- the
    primary key holds one row per line per version.
    """

    __tablename__ = "denial_classification"

    service_line_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    taxonomy_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claim_payment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claim_payment.claim_payment_id"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    preventability: Mapped[str] = mapped_column(String(24), nullable=False)
    #: The reason that decided the category (PD-3: never a CARC without its group).
    cas_group_code: Mapped[str] = mapped_column(String(2), nullable=False)
    carc_code: Mapped[str] = mapped_column(String(8), nullable=False)
    classified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The line, its client and its posting, as one fact: a classification
        # cannot sit under a client the line does not belong to (PD-20), and
        # the insert-only grant means it could never be moved back.
        ForeignKeyConstraint(
            ["service_line_id", "client_id", "claim_payment_id"],
            [
                "remittance_service_line.service_line_id",
                "remittance_service_line.client_id",
                "remittance_service_line.claim_payment_id",
            ],
            name="fk_denial_classification_line",
        ),
        CheckConstraint(f"category IN ({CATEGORIES})", name="category_is_known"),
        CheckConstraint(f"preventability IN ({PREVENTABILITY})", name="preventability_is_known"),
        CheckConstraint(
            "cas_group_code IN ('CO', 'PR', 'OA', 'PI', 'CR')", name="cas_group_code_is_valid"
        ),
        Index("ix_denial_classification_client_version", "client_id", "taxonomy_version"),
        Index("ix_denial_classification_client_category", "client_id", "category"),
        Index("ix_denial_classification_claim_payment", "claim_payment_id"),
    )
