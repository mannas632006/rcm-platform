"""NCCI releases and their rows (M4.3, ADR-0008). Insert-only by grant."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from rcm.platform.db import Base


class NcciRelease(Base):
    __tablename__ = "ncci_release"

    #: "<kind>:<label>", e.g. "PTP:2026Q1".
    release_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(3), nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Of the file as loaded: the same label from a different file is refused.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    loaded_by: Mapped[str] = mapped_column(String(128), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('MUE', 'PTP')", name="ck_ncci_release_kind"),
        CheckConstraint("release_id = kind || ':' || label", name="ck_ncci_release_id"),
        CheckConstraint("row_count > 0", name="ck_ncci_release_rows"),
    )


class NcciPtpEdit(Base):
    __tablename__ = "ncci_ptp_edit"

    release_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ncci_release.release_id"), primary_key=True
    )
    column_1: Mapped[str] = mapped_column(String(5), primary_key=True)
    column_2: Mapped[str] = mapped_column(String(5), primary_key=True)
    effective_date: Mapped[date] = mapped_column(Date, primary_key=True)
    deletion_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    modifier_indicator: Mapped[str] = mapped_column(String(1), nullable=False)

    __table_args__ = (
        CheckConstraint("modifier_indicator IN ('0', '1', '9')", name="ck_ncci_ptp_indicator"),
        CheckConstraint(
            "deletion_date IS NULL OR deletion_date > effective_date", name="ck_ncci_ptp_dates"
        ),
    )


class NcciMue(Base):
    __tablename__ = "ncci_mue"

    release_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ncci_release.release_id"), primary_key=True
    )
    code: Mapped[str] = mapped_column(String(5), primary_key=True)
    mue_value: Mapped[Decimal] = mapped_column(Numeric(9, 2), nullable=False)
    adjudication_indicator: Mapped[str] = mapped_column(String(1), nullable=False)

    __table_args__ = (
        CheckConstraint("mue_value > 0", name="ck_ncci_mue_value"),
        CheckConstraint("adjudication_indicator IN ('1', '2', '3')", name="ck_ncci_mue_indicator"),
    )
