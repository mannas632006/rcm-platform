"""Serving's tables (M6.4, M6.6-M6.8, ADR-0028, migration 0029).

Each is insert-only by grant. A deployment's status is its latest row; a
prediction is made once per line per model version; a monitor run is what one
drift check measured. None is ever edited, because each is what a later
question -- why was this scored, why was the model withdrawn -- is answered
from.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base

#: SRS 9.1's ids for the models that are served.
DENIAL_RISK: Final = "ML-1"
RECOVERY: Final = "ML-3"


class Status(StrEnum):
    #: Scores are recorded and never used (FR-ML-04).
    SHADOW = "SHADOW"
    #: Scores are used.
    ACTIVE = "ACTIVE"
    #: Withdrawn: the work it informed is on rules only (FR-ML-08).
    REVERTED = "REVERTED"
    #: Replaced by a later version.
    RETIRED = "RETIRED"


class PredictionStatus(StrEnum):
    SCORED = "SCORED"
    #: No score: something the model needs was missing. Never a guess
    #: (FR-ML-06).
    DEGRADED = "DEGRADED"


class Severity(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class MonitorAction(StrEnum):
    NONE = "NONE"
    REVERTED = "REVERTED"


class Deployment(Base):
    __tablename__ = "ml_deployment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SHADOW', 'ACTIVE', 'REVERTED', 'RETIRED')", name="status_known"
        ),
        CheckConstraint("length(trim(reason)) > 0", name="reason_given"),
        CheckConstraint("length(trim(changed_by)) > 0", name="changed_by_named"),
        ForeignKeyConstraint(
            ["model_name", "version"],
            ["ml_model_version.model_name", "ml_model_version.version"],
        ),
        Index("ix_ml_deployment_model_name", "model_name", "deployment_id"),
    )

    #: Rows are read in this order: the latest row of a version is its status.
    deployment_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: `user:<subject>`, a tool, or `monitor:auto` for an unattended revert.
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Prediction(Base):
    __tablename__ = "ml_prediction"
    __table_args__ = (
        CheckConstraint("mode IN ('SHADOW', 'ACTIVE')", name="mode_known"),
        CheckConstraint("status IN ('SCORED', 'DEGRADED')", name="status_known"),
        CheckConstraint(
            "(status = 'SCORED' AND score IS NOT NULL AND score >= 0 AND score <= 1 "
            "AND decision_id IS NOT NULL AND service_line_id IS NOT NULL) "
            "OR (status = 'DEGRADED' AND score IS NULL AND reason IS NOT NULL)",
            name="scored_or_explained",
        ),
        UniqueConstraint("model_version_id", "service_line_id"),
        Index("ix_ml_prediction_version_predicted_at", "model_version_id", "predicted_at"),
        Index("ix_ml_prediction_claim_id", "claim_id"),
    )

    prediction_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("ml_model_version.model_version_id"), nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The version's status when the score was made: SHADOW or ACTIVE.
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: Null only for a claim the warehouse holds no row for at all.
    service_line_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    #: Why there is no score.
    reason: Mapped[str | None] = mapped_column(Text)
    #: Inputs, output and SHAP drivers (FR-ML-05).
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("decision_record.decision_id")
    )
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MonitorRun(Base):
    __tablename__ = "ml_monitor_run"
    __table_args__ = (
        CheckConstraint("severity IN ('OK', 'WARNING', 'CRITICAL')", name="severity_known"),
        CheckConstraint("action IN ('NONE', 'REVERTED')", name="action_known"),
    )

    monitor_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_name: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    thresholds_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    breaches: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
