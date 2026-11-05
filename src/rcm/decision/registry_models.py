"""Payer registry entries (M4.10, DOC-02 9, ADR-0014).

Insert-only by grant (migration 0024). A change or a re-verification is a
new version; the old one stays, so "which entry applied to this claim" can
always be answered (BR-REG-03).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.domain.payer import Payer as _Payer  # noqa: F401 - FK target
from rcm.platform.db import Base

#: "Any": an entry for every plan type, or every procedure, of the payer.
WILDCARD = "*"


class PayerRegistryEntry(Base):
    __tablename__ = "payer_registry_entry"

    entry_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: PD-8: the payer's identifier, never its name.
    payer_id: Mapped[str] = mapped_column(String(80), ForeignKey("payer.payer_id"), nullable=False)
    plan_type: Mapped[str] = mapped_column(String(64), nullable=False)
    cpt: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: None: not recorded. Never read as "not required" -- the registry
    #: asserts only what someone verified.
    prior_auth_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: BR-REG-01. The day a person confirmed the entry against the payer.
    last_verified_date: Mapped[date] = mapped_column(Date, nullable=False)
    verified_by: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Why this version exists: the change, or "re-verified".
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(128), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "payer_id", "plan_type", "cpt", "version", name="uq_payer_registry_entry_version"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("length(trim(verified_by)) > 0", name="names_its_verifier"),
        CheckConstraint("length(trim(reason)) > 0", name="states_a_reason"),
        CheckConstraint("length(plan_type) > 0 AND length(cpt) > 0", name="keys_are_named"),
        Index("ix_payer_registry_entry_payer", "payer_id", "cpt", "plan_type", "version"),
    )
