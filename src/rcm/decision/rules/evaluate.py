"""Evaluate a claim against a ruleset (M4.2, SRS 8.2, ADR-0007).

Pure: facts in, findings and a verdict out. No database, no clock, so a
backtest (M4.4) runs exactly the code that scrubs a live claim.

Three-valued: a condition is true, false or INDETERMINATE when the claim
lacks a value it needs. INDETERMINATE is never read as "no problem"
(PD-18); `exists` / `not_exists` ask about absence on purpose.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Final, Protocol

from rcm.decision.rules.definition import (
    CLAIM_FIELDS,
    LAYER_ORDER,
    LINE_FIELDS,
    LIVE_STATUSES,
    AllOf,
    Condition,
    Layer,
    Leaf,
    Not,
    Op,
    Rule,
    RuleStatus,
    Severity,
)
from rcm.platform.errors import ValidationFailedError


class Outcome(StrEnum):
    FLAGGED = "FLAGGED"
    #: The claim lacks a value the rule needs.
    INDETERMINATE = "INDETERMINATE"


class Mode(StrEnum):
    """What a finding may do, from the rule's severity and status together."""

    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"
    INFORMATIONAL = "INFORMATIONAL"
    #: Recorded and compared, never acted on (FR-RULES-06).
    SHADOW = "SHADOW"


class Verdict(StrEnum):
    PASS = "PASS"  # noqa: S105 -- a verdict, not a credential
    ADVISE = "ADVISE"
    BLOCK = "BLOCK"


class _Truth(Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LineFacts:
    number: int
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ClaimFacts:
    """Every field a rule may name, for one claim. A missing key is refused
    here: a builder that forgot a field would make every rule on it
    indeterminate, or skip it."""

    claim: Mapping[str, object]
    lines: Sequence[LineFacts]

    def __post_init__(self) -> None:
        if set(self.claim) != CLAIM_FIELDS:
            raise ValidationFailedError(
                field="facts.claim", reason="exactly the claim fields rules may name"
            )
        for line in self.lines:
            if set(line.values) != LINE_FIELDS:
                raise ValidationFailedError(
                    field=f"facts.lines[{line.number}]",
                    reason="exactly the line fields rules may name",
                )


@dataclass(frozen=True, slots=True)
class ActiveRule:
    """A rule version as it stands in a ruleset."""

    rule: Rule
    version: int
    status: RuleStatus

    @property
    def mode(self) -> Mode:
        if self.status is RuleStatus.SHADOW:
            return Mode.SHADOW
        if self.rule.severity is Severity.INFORMATIONAL:
            return Mode.INFORMATIONAL
        if self.status is RuleStatus.BLOCKING and self.rule.severity is Severity.BLOCKING:
            return Mode.BLOCKING
        return Mode.ADVISORY


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    rule_version: int
    layer: Layer
    mode: Mode
    outcome: Outcome
    #: None for a rule checked once per claim.
    line_number: int | None
    message: str
    route_to: str
    #: What a built-in check matched, e.g. the NCCI edit. Empty for a rule,
    #: which its id and version already name.
    reference: str = ""


class Check(Protocol):
    """A built-in check that the condition language cannot express, such as
    NCCI pairs across lines (ADR-0008). Runs in its layer, after the rules."""

    @property
    def layer(self) -> Layer: ...

    def findings(self, facts: ClaimFacts) -> list[Finding]: ...


@dataclass(frozen=True, slots=True)
class Evaluation:
    verdict: Verdict
    findings: tuple[Finding, ...]
    layers_run: tuple[Layer, ...]


_ACTS: Final = frozenset({Mode.BLOCKING, Mode.ADVISORY})


def _compare(leaf: Leaf, actual: object) -> _Truth:
    if leaf.op is Op.EXISTS:
        return _Truth.TRUE if actual is not None else _Truth.FALSE
    if leaf.op is Op.NOT_EXISTS:
        return _Truth.TRUE if actual is None else _Truth.FALSE
    if actual is None:
        return _Truth.UNKNOWN
    expected = leaf.value
    match leaf.op:
        case Op.EQ:
            hit = actual == expected
        case Op.NE:
            hit = actual != expected
        case Op.IN:
            hit = actual in expected  # type: ignore[operator]
        case Op.NOT_IN:
            hit = actual not in expected  # type: ignore[operator]
        case Op.CONTAINS:
            hit = expected in actual  # type: ignore[operator]
        case Op.NOT_CONTAINS:
            hit = expected not in actual  # type: ignore[operator]
        case Op.GT:
            hit = actual > expected  # type: ignore[operator]
        case Op.GTE:
            hit = actual >= expected  # type: ignore[operator]
        case Op.LT:
            hit = actual < expected  # type: ignore[operator]
        case Op.LTE:
            hit = actual <= expected  # type: ignore[operator]
    return _Truth.TRUE if hit else _Truth.FALSE


def _truth(condition: Condition, values: Mapping[str, object]) -> _Truth:
    if isinstance(condition, Leaf):
        return _compare(condition, values[condition.field])
    if isinstance(condition, Not):
        inner = _truth(condition.item, values)
        if inner is _Truth.UNKNOWN:
            return inner
        return _Truth.FALSE if inner is _Truth.TRUE else _Truth.TRUE
    results = [_truth(item, values) for item in condition.items]
    # Kleene logic: a decisive branch settles it whatever the unknowns are.
    decisive = _Truth.FALSE if isinstance(condition, AllOf) else _Truth.TRUE
    if decisive in results:
        return decisive
    if _Truth.UNKNOWN in results:
        return _Truth.UNKNOWN
    return _Truth.TRUE if isinstance(condition, AllOf) else _Truth.FALSE


def _applies(active: ActiveRule, facts: ClaimFacts) -> bool:
    payers = active.rule.scope.payer_ids
    return payers is None or facts.claim["claim.payer_id"] in payers


def _check(active: ActiveRule, facts: ClaimFacts) -> list[Finding]:
    rule = active.rule
    targets: list[tuple[int | None, Mapping[str, object]]]
    if rule.is_line_level:
        cpts = rule.scope.cpts
        targets = [
            (line.number, {**facts.claim, **line.values})
            for line in facts.lines
            if cpts is None or line.values["line.procedure_code"] in cpts
        ]
    else:
        targets = [(None, facts.claim)]

    found: list[Finding] = []
    for number, values in targets:
        truth = _truth(rule.condition, values)
        if truth is _Truth.FALSE:
            continue
        found.append(
            Finding(
                rule_id=rule.rule_id,
                rule_version=active.version,
                layer=rule.layer,
                mode=active.mode,
                outcome=Outcome.FLAGGED if truth is _Truth.TRUE else Outcome.INDETERMINATE,
                line_number=number,
                message=rule.action.message,
                route_to=rule.action.route_to,
            )
        )
    return found


def evaluate(
    rules: Sequence[ActiveRule], facts: ClaimFacts, *, checks: Sequence[Check] = ()
) -> Evaluation:
    """Every layer, in order, every time (SRS 8.2, ADR-0007 Decision 6)."""
    for active in rules:
        if active.status not in LIVE_STATUSES:
            raise ValidationFailedError(
                field="rules", reason=f"{active.rule.rule_id} is {active.status}, not live"
            )

    findings: list[Finding] = []
    for layer in LAYER_ORDER:
        in_layer = sorted(
            (r for r in rules if r.rule.layer is layer),
            key=lambda r: (r.rule.rule_id, r.version),
        )
        for active in in_layer:
            if _applies(active, facts):
                findings.extend(_check(active, facts))
        for check in checks:
            if check.layer is layer:
                findings.extend(check.findings(facts))

    acting = {f.mode for f in findings if f.mode in _ACTS}
    if Mode.BLOCKING in acting:
        verdict = Verdict.BLOCK
    elif Mode.ADVISORY in acting:
        verdict = Verdict.ADVISE
    else:
        verdict = Verdict.PASS
    return Evaluation(verdict=verdict, findings=tuple(findings), layers_run=LAYER_ORDER)
