"""Rule versions and published rulesets (M4.1, ADR-0007).

Write-once by grant, as the audit tables are (migration 0017): the
application role can insert rule versions and change only their `status`,
and can only insert rulesets.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from rcm.decision.ncci_models import NcciRelease as _NcciRelease  # noqa: F401 - FK target
from rcm.decision.rules.definition import LIVE_STATUSES, Layer, RuleStatus, Severity
from rcm.platform.db import Base


def _one_of(column: str, values: Iterable[object]) -> str:
    listed = ", ".join(f"'{v}'" for v in sorted(str(v) for v in values))
    return f"{column} IN ({listed})"


LIVE_SQL: Final = _one_of("status", LIVE_STATUSES)


class RuleVersion(Base):
    __tablename__ = "rule_version"

    rule_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Copied from the document so rules can be listed by layer and
    #: severity; the document stays the source.
    layer: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The one mutable column (FR-RULES-06), moved by M4.5's promotion path.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=str(RuleStatus.DRAFT), server_default="DRAFT"
    )
    #: The rule as written. Re-parsed on load, so a stored rule is checked
    #: by the same code that accepted it.
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_rule_version_positive"),
        CheckConstraint(_one_of("layer", Layer), name="ck_rule_version_layer"),
        CheckConstraint(_one_of("severity", Severity), name="ck_rule_version_severity"),
        CheckConstraint(_one_of("status", RuleStatus), name="ck_rule_version_status"),
        # One live version per rule: a ruleset never holds two answers to
        # "what does this rule say".
        Index(
            "uq_rule_version_one_live",
            "rule_id",
            unique=True,
            postgresql_where=text(LIVE_SQL),
        ),
    )


class Ruleset(Base):
    __tablename__ = "ruleset"

    ruleset_version: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    #: SHA-256 over the members and their documents: the same rules give the
    #: same digest, whatever the version number.
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    published_by: Mapped[str] = mapped_column(String(128), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: The NCCI releases pinned with the rules (ADR-0008). None: no release
    #: was loaded when this ruleset was published.
    ncci_ptp_release: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("ncci_release.release_id"), nullable=True
    )
    ncci_mue_release: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("ncci_release.release_id"), nullable=True
    )

    __table_args__ = (CheckConstraint("rule_count >= 0", name="ck_ruleset_rule_count"),)


class RuleBacktest(Base):
    """One backtest of one rule version against one client's history
    (M4.4, ADR-0009). Insert-only: M4.5 promotes on what is recorded here."""

    __tablename__ = "rule_backtest"

    backtest_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rule_id: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    claims_evaluated: Mapped[int] = mapped_column(Integer, nullable=False)
    flagged: Mapped[int] = mapped_column(Integer, nullable=False)
    true_positives: Mapped[int] = mapped_column(Integer, nullable=False)
    false_positives: Mapped[int] = mapped_column(Integer, nullable=False)
    missed_denials: Mapped[int] = mapped_column(Integer, nullable=False)
    indeterminate: Mapped[int] = mapped_column(Integer, nullable=False)
    without_outcome: Mapped[int] = mapped_column(Integer, nullable=False)
    #: None when nothing was flagged: no evidence of accuracy, not 0%.
    fp_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    ran_by: Mapped[str] = mapped_column(String(128), nullable=False)
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["rule_id", "version"],
            ["rule_version.rule_id", "rule_version.version"],
            name="fk_rule_backtest_rule_version",
        ),
        CheckConstraint(
            "flagged = true_positives + false_positives AND flagged <= claims_evaluated",
            name="ck_rule_backtest_counts",
        ),
        CheckConstraint(
            "(flagged = 0 AND fp_rate IS NULL) OR (flagged > 0 AND fp_rate BETWEEN 0 AND 1)",
            name="ck_rule_backtest_fp_rate",
        ),
        Index("ix_rule_backtest_rule", "rule_id", "version", "ran_at"),
    )


class RuleStatusChange(Base):
    """One step of one rule version along the lifecycle (M4.5, ADR-0010).
    Insert-only: the history of who promoted what, on what evidence."""

    __tablename__ = "rule_status_change"

    change_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: Order of changes made within one transaction, which share `changed_at`.
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    from_status: Mapped[str] = mapped_column(String(16), nullable=False)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    backtest_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("rule_backtest.backtest_id"), nullable=True
    )
    fp_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["rule_id", "version"],
            ["rule_version.rule_id", "rule_version.version"],
            name="fk_rule_status_change_rule_version",
        ),
        CheckConstraint("length(trim(reason)) > 0", name="ck_rule_status_change_reason"),
        # SHADOW and BLOCKING rest on a backtest; the record must name it.
        CheckConstraint(
            "to_status NOT IN ('SHADOW', 'BLOCKING') OR backtest_id IS NOT NULL",
            name="ck_rule_status_change_evidence",
        ),
        Index("ix_rule_status_change_rule", "rule_id", "version", "sequence"),
    )


class RulesetMember(Base):
    __tablename__ = "ruleset_member"

    ruleset_version: Mapped[int] = mapped_column(
        Integer, ForeignKey("ruleset.ruleset_version"), primary_key=True
    )
    rule_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The status when published; a later change waits for the next publish.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["rule_id", "version"],
            ["rule_version.rule_id", "rule_version.version"],
            name="fk_ruleset_member_rule_version",
        ),
        CheckConstraint(LIVE_SQL, name="ck_ruleset_member_status"),
    )
