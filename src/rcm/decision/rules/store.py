"""Rule versions in, rulesets out (M4.1, ADR-0007).

A rule is checked by `parse_rule` before it is stored and again when it is
published and loaded, so the code that evaluates a claim never sees a rule
the current validator would refuse.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rcm.decision.rules.definition import LIVE_STATUSES, RuleStatus, parse_rule
from rcm.decision.rules.evaluate import ActiveRule
from rcm.decision.rules.models import Ruleset, RulesetMember, RuleVersion
from rcm.platform.errors import (
    ConcurrencyConflictError,
    ResourceNotFoundError,
    ValidationFailedError,
)


@dataclass(frozen=True, slots=True)
class PublishedRuleset:
    version: int
    digest: str
    rule_count: int


@dataclass(frozen=True, slots=True)
class LoadedRuleset:
    version: int
    digest: str
    rules: tuple[ActiveRule, ...]


def _as_json(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        stored: dict[str, Any] = json.loads(json.dumps(document))
    except (TypeError, ValueError):
        raise ValidationFailedError(field="document", reason="plain JSON") from None
    return stored


def create_rule_version(session: Session, *, document: Mapping[str, Any], created_by: str) -> int:
    """Store a new version of a rule, as a draft. Returns its version."""
    if not created_by.strip():
        raise ValidationFailedError(field="created_by", reason="name the author")
    stored = _as_json(document)
    rule = parse_rule(stored)
    latest = session.scalar(
        select(func.max(RuleVersion.version)).where(RuleVersion.rule_id == rule.rule_id)
    )
    version = (latest or 0) + 1
    try:
        with session.begin_nested():
            session.add(
                RuleVersion(
                    rule_id=rule.rule_id,
                    version=version,
                    layer=str(rule.layer),
                    severity=str(rule.severity),
                    status=str(RuleStatus.DRAFT),
                    document=stored,
                    created_by=created_by,
                )
            )
    except IntegrityError:
        # Two authors saved the same rule at once; the second retries on
        # top of the first rather than overwriting it.
        raise ConcurrencyConflictError(resource="rule_version") from None
    return version


def _digest(rows: list[RuleVersion]) -> str:
    canonical = json.dumps(
        [
            {"rule_id": r.rule_id, "version": r.version, "status": r.status, "rule": r.document}
            for r in sorted(rows, key=lambda r: r.rule_id)
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def publish_ruleset(session: Session, *, published_by: str) -> PublishedRuleset:
    """Snapshot every live rule version as the next ruleset (ADR-0007 Decision 2)."""
    if not published_by.strip():
        raise ValidationFailedError(field="published_by", reason="name the publisher")
    rows = list(
        session.scalars(
            select(RuleVersion)
            .where(RuleVersion.status.in_([str(s) for s in LIVE_STATUSES]))
            .order_by(RuleVersion.rule_id)
        ).all()
    )
    for row in rows:
        # A rule the current validator refuses is not published (PD-18).
        parse_rule(row.document)

    ruleset = Ruleset(digest=_digest(rows), rule_count=len(rows), published_by=published_by)
    session.add(ruleset)
    session.flush()
    session.add_all(
        RulesetMember(
            ruleset_version=ruleset.ruleset_version,
            rule_id=row.rule_id,
            version=row.version,
            status=row.status,
        )
        for row in rows
    )
    session.flush()
    return PublishedRuleset(
        version=ruleset.ruleset_version, digest=ruleset.digest, rule_count=len(rows)
    )


def load_ruleset(session: Session, *, version: int | None = None) -> LoadedRuleset:
    """The latest ruleset, or the one named. None published is an error,
    never an empty ruleset: no rules is not "no problems" (PD-18)."""
    query = select(Ruleset)
    if version is None:
        query = query.order_by(Ruleset.ruleset_version.desc()).limit(1)
    else:
        query = query.where(Ruleset.ruleset_version == version)
    ruleset = session.scalar(query)
    if ruleset is None:
        raise ResourceNotFoundError(resource="ruleset")

    in_ruleset = RulesetMember.ruleset_version == ruleset.ruleset_version
    members = session.scalars(
        select(RulesetMember).where(in_ruleset).order_by(RulesetMember.rule_id)
    ).all()
    documents = {
        (row.rule_id, row.version): row.document
        for row in session.scalars(
            select(RuleVersion)
            .join(
                RulesetMember,
                (RuleVersion.rule_id == RulesetMember.rule_id)
                & (RuleVersion.version == RulesetMember.version),
            )
            .where(in_ruleset)
        )
    }
    return LoadedRuleset(
        version=ruleset.ruleset_version,
        digest=ruleset.digest,
        rules=tuple(
            ActiveRule(
                rule=parse_rule(documents[(m.rule_id, m.version)]),
                version=m.version,
                status=RuleStatus(m.status),
            )
            for m in members
        ),
    )
