"""Rule versions and published rulesets (M4.1, ADR-0007).

Write-once by grant, as the audit tables are (migration 0017): the
application role can insert rule versions and change only their `status`,
and can only insert rulesets.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

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

    __table_args__ = (CheckConstraint("rule_count >= 0", name="ck_ruleset_rule_count"),)


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
