"""The model registry's table (M6.1, ADR-0026, migration 0028).

One row per trained model version, accepted or rejected: what it was trained
on, by which code, how it scored, and the model itself. Insert-only by grant.
A trained model's record is evidence (FR-ML-03), and evidence is not edited.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base


class Verdict(StrEnum):
    #: Beat the rules baseline and the current model: may enter shadow.
    ACCEPTED = "ACCEPTED"
    #: Did not. Kept, with the numbers that rejected it.
    REJECTED = "REJECTED"


class DataSource(StrEnum):
    #: CLAUDE.md section 8: never to run in production.
    SYNTHETIC = "SYNTHETIC"
    REAL = "REAL"


class ModelVersion(Base):
    __tablename__ = "ml_model_version"
    __table_args__ = (
        UniqueConstraint("model_name", "version"),
        CheckConstraint("verdict IN ('ACCEPTED', 'REJECTED')", name="verdict_known"),
        CheckConstraint("data_source IN ('REAL', 'SYNTHETIC')", name="data_source_known"),
        CheckConstraint("char_length(data_hash) = 64", name="data_hash_is_sha256"),
    )

    model_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: SRS 9.1's id: ML-1, ML-2, ML-3, ML-5.
    model_name: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Why, with the numbers: "PR-AUC 0.41 vs rules baseline 0.18".
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    data_source: Mapped[str] = mapped_column(String(16), nullable=False)
    data_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    #: True when the working tree had uncommitted changes: the commit alone
    #: does not then reproduce the model.
    code_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    thresholds_version: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    baseline: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The temporal split's dates (FR-ML-02).
    windows: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The features and the frozen category vocabulary.
    encoding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The model, in LightGBM's text format.
    artefact: Mapped[str] = mapped_column(Text, nullable=False)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64))
    trained_by: Mapped[str] = mapped_column(String(128), nullable=False)
    trained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
