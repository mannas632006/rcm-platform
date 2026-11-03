"""Deterministic auto-correction, and the loop around it (M4.7, ADR-0011).

BR-M7-03: auto-correction only for deterministic fixes. Each fix below is
formatting: it changes how a value is written, never what it says. Nothing
here adds a modifier, and the corrector refuses any fix that would put 25,
59 or X{EPSU} on a line, whichever fix tried (PD-16).

SRS 8.2: a blocking finding that can be fixed is fixed, logged, and the
claim is evaluated again from Layer 1. Pure: facts in, facts out.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from rcm.decision.rules.definition import PROTECTED_MODIFIERS
from rcm.decision.rules.evaluate import (
    ActiveRule,
    Check,
    ClaimFacts,
    Evaluation,
    LineFacts,
    Mode,
    Outcome,
    evaluate,
)
from rcm.platform.errors import ManualCodingRequiredError, ValidationFailedError

#: Evaluations per claim, at most; fixes happen between them. A claim still
#: blocking after the last goes to a person.
MAX_ROUNDS: Final = 3


def _uppercase(values: list[str]) -> list[str]:
    return [value.upper() for value in values]


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _strip_decimal(values: list[str]) -> list[str]:
    return [value.replace(".", "") for value in values]


#: Line fixes act on `line.modifiers`; claim fixes on `claim.diagnoses`.
LINE_FIXERS: Final[dict[str, Callable[[list[str]], list[str]]]] = {
    "UPPERCASE_MODIFIERS": _uppercase,
    "DEDUPLICATE_MODIFIERS": _deduplicate,
}
CLAIM_FIXERS: Final[dict[str, Callable[[list[str]], list[str]]]] = {
    "STRIP_DIAGNOSIS_DECIMAL": _strip_decimal,
}


@dataclass(frozen=True, slots=True)
class Correction:
    fix: str
    field: str
    #: None for a claim-level fix.
    line_number: int | None
    before: list[str]
    after: list[str]


@dataclass(frozen=True, slots=True)
class Corrected:
    evaluation: Evaluation
    facts: ClaimFacts
    corrections: tuple[Correction, ...]
    rounds: int


def _texts(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValidationFailedError(field=field, reason="a list of codes")
    return [str(v) for v in value]


def apply_fix(
    facts: ClaimFacts, fix: str, line_number: int | None
) -> tuple[ClaimFacts, Correction | None]:
    """The facts with one fix applied, and what changed (None: nothing)."""
    if fix in CLAIM_FIXERS:
        field = "claim.diagnoses"
        before = _texts(facts.claim[field], field)
        after = CLAIM_FIXERS[fix](list(before))
        if after == before:
            return facts, None
        claim = {**facts.claim, field: after}
        return replace(facts, claim=claim), Correction(fix, field, None, before, after)

    if fix not in LINE_FIXERS:
        raise ValidationFailedError(field="fix", reason=f"{fix} is not a catalogued fix")
    target = [line for line in facts.lines if line.number == line_number]
    if not target:
        raise ValidationFailedError(field="line_number", reason="no such line on the claim")
    field = "line.modifiers"
    before = _texts(target[0].values[field], field)
    after = LINE_FIXERS[fix](list(before))
    added = {m.upper() for m in after} - {m.upper() for m in before}
    if added & PROTECTED_MODIFIERS:
        # ADR-0011 Decision 2: the catalogue never adds a modifier, and this
        # holds even if a fix is ever written wrongly.
        raise ManualCodingRequiredError(field=field, line_number=line_number)
    if after == before:
        return facts, None
    lines = tuple(
        LineFacts(number=line.number, values={**line.values, field: after})
        if line.number == line_number
        else line
        for line in facts.lines
    )
    return replace(facts, lines=lines), Correction(fix, field, line_number, before, after)


def run_pipeline(
    rules: Sequence[ActiveRule],
    facts: ClaimFacts,
    *,
    checks: Sequence[Check] = (),
) -> Corrected:
    """Evaluate; fix the blocking findings that can be fixed; evaluate again
    from Layer 1. Stops at a clean claim, a finding no fix covers, a fix that
    changes nothing, or MAX_ROUNDS evaluations."""
    corrections: list[Correction] = []
    rounds = 0
    while True:
        rounds += 1
        evaluation = evaluate(rules, facts, checks=checks)
        blocking = [f for f in evaluation.findings if f.mode is Mode.BLOCKING]
        fixable = all(f.outcome is Outcome.FLAGGED and f.fix is not None for f in blocking)
        if not blocking or not fixable or rounds == MAX_ROUNDS:
            break
        changed = False
        for finding in blocking:
            assert finding.fix is not None
            facts, change = apply_fix(facts, finding.fix, finding.line_number)
            if change is not None:
                corrections.append(change)
                changed = True
        if not changed:
            break
    return Corrected(
        evaluation=evaluation, facts=facts, corrections=tuple(corrections), rounds=rounds
    )
