"""A rule, as data, checked when it is written (M4.1, SRS 8.1, ADR-0007).

Every check that can be made before a rule runs is made here: a field the
scrubber does not know, an operator that does not fit the field, a value of
the wrong type. A rule that passes `parse_rule` cannot fail at evaluation
for any reason but the claim's own data -- and that case is INDETERMINATE,
never a pass (ADR-0007 Decision 5).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from rcm.platform.errors import ValidationFailedError


class Layer(StrEnum):
    STRUCTURAL = "STRUCTURAL"
    DEMOGRAPHIC = "DEMOGRAPHIC"
    CLINICAL = "CLINICAL"
    PAYER_SPECIFIC = "PAYER_SPECIFIC"
    TEMPORAL = "TEMPORAL"
    LEARNED = "LEARNED"


#: SRS 8.2. The order is the rule, and a test holds it.
LAYER_ORDER: Final[tuple[Layer, ...]] = (
    Layer.STRUCTURAL,
    Layer.DEMOGRAPHIC,
    Layer.CLINICAL,
    Layer.PAYER_SPECIFIC,
    Layer.TEMPORAL,
    Layer.LEARNED,
)


class Severity(StrEnum):
    """What the author says the rule may do at most."""

    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"
    INFORMATIONAL = "INFORMATIONAL"


class RuleStatus(StrEnum):
    """FR-RULES-06's lifecycle. Moves only through M4.5's promotion path."""

    DRAFT = "DRAFT"
    BACKTESTING = "BACKTESTING"
    SHADOW = "SHADOW"
    ADVISORY = "ADVISORY"
    BLOCKING = "BLOCKING"
    DEPRECATED = "DEPRECATED"


#: A ruleset holds these, and nothing else is evaluated.
LIVE_STATUSES: Final[frozenset[RuleStatus]] = frozenset(
    {RuleStatus.SHADOW, RuleStatus.ADVISORY, RuleStatus.BLOCKING}
)


class Op(StrEnum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"


class FieldType(StrEnum):
    TEXT = "text"
    MONEY = "money"
    NUMBER = "number"
    INTEGER = "integer"
    DATE = "date"
    BOOLEAN = "boolean"
    TEXT_LIST = "text_list"


#: What a condition may name. None of it is PHI (PD-13), so a finding's
#: inputs are recorded in full. A rule needing PHI waits for M4.10, where
#: the read is audited (PD-2).
FIELDS: Final[Mapping[str, FieldType]] = {
    "claim.payer_id": FieldType.TEXT,
    "claim.claim_frequency_code": FieldType.TEXT,
    "claim.claim_filing_code": FieldType.TEXT,
    "claim.place_of_service": FieldType.TEXT,
    "claim.total_charge": FieldType.MONEY,
    "claim.date_of_service": FieldType.DATE,
    "claim.has_prior_auth": FieldType.BOOLEAN,
    "claim.diagnoses": FieldType.TEXT_LIST,
    "claim.procedure_codes": FieldType.TEXT_LIST,
    "claim.line_count": FieldType.INTEGER,
    "line.procedure_code": FieldType.TEXT,
    "line.modifiers": FieldType.TEXT_LIST,
    "line.units": FieldType.NUMBER,
    "line.charge_amount": FieldType.MONEY,
    "line.service_date": FieldType.DATE,
}

CLAIM_FIELDS: Final = frozenset(f for f in FIELDS if f.startswith("claim."))
LINE_FIELDS: Final = frozenset(f for f in FIELDS if f.startswith("line."))

_ORDERED: Final = frozenset({FieldType.MONEY, FieldType.NUMBER, FieldType.INTEGER, FieldType.DATE})
_SCALAR: Final = frozenset(FieldType) - {FieldType.TEXT_LIST}

#: Which operators fit which field types.
_OPS: Final[Mapping[Op, frozenset[FieldType]]] = {
    Op.EQ: _SCALAR,
    Op.NE: _SCALAR,
    Op.IN: _SCALAR - {FieldType.BOOLEAN},
    Op.NOT_IN: _SCALAR - {FieldType.BOOLEAN},
    Op.CONTAINS: frozenset({FieldType.TEXT_LIST}),
    Op.NOT_CONTAINS: frozenset({FieldType.TEXT_LIST}),
    Op.GT: _ORDERED,
    Op.GTE: _ORDERED,
    Op.LT: _ORDERED,
    Op.LTE: _ORDERED,
    Op.EXISTS: frozenset(FieldType),
    Op.NOT_EXISTS: frozenset(FieldType),
}

#: The work a finding can be routed to. Names of `rcm.work`'s kinds, which
#: this layer cannot import.
ROUTES: Final = frozenset({"SCRUB_EXCEPTION", "AUTH_PROBLEM", "ELIGIBILITY_PROBLEM"})

_RULE_ID: Final = re.compile(r"[A-Z0-9][A-Z0-9_]{0,119}")
_SCOPE_KEYS: Final = {"payer_id", "cpt"}

Value = str | Decimal | int | date | bool | tuple[Any, ...] | None


@dataclass(frozen=True, slots=True)
class Leaf:
    field: str
    op: Op
    value: Value = None


@dataclass(frozen=True, slots=True)
class AllOf:
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class AnyOf:
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Not:
    item: Condition


Condition = Leaf | AllOf | AnyOf | Not


@dataclass(frozen=True, slots=True)
class Scope:
    #: None: every payer. Never empty -- an empty list is refused, because
    #: "applies to no payer" and "applies to all" read alike.
    payer_ids: frozenset[str] | None = None
    cpts: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class Action:
    message: str
    route_to: str
    auto_correct: bool = False


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    layer: Layer
    severity: Severity
    scope: Scope
    condition: Condition
    action: Action
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_line_level(self) -> bool:
        """Checked once per line when the condition names any line field."""
        return any(leaf.field in LINE_FIELDS for leaf in leaves(self.condition))


def leaves(condition: Condition) -> list[Leaf]:
    if isinstance(condition, Leaf):
        return [condition]
    if isinstance(condition, Not):
        return leaves(condition.item)
    return [leaf for item in condition.items for leaf in leaves(item)]


def _refuse(where: str, reason: str) -> ValidationFailedError:
    return ValidationFailedError(field=where, reason=reason)


def _enum[E: StrEnum](kind: type[E], raw: object, where: str) -> E:
    try:
        return kind(str(raw))
    except ValueError:
        raise _refuse(where, f"one of {', '.join(kind)}") from None


def _codes(raw: object, where: str) -> frozenset[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw or not all(isinstance(c, str) and c for c in raw):
        raise _refuse(where, "a non-empty list of codes")
    return frozenset(raw)


def _scalar(kind: FieldType, raw: object, where: str) -> Value:
    """One value of a field's type. Floats are refused: money and units are
    exact, and 10.1 is not."""
    match kind:
        case FieldType.TEXT if isinstance(raw, str) and raw:
            return raw
        case FieldType.MONEY | FieldType.NUMBER if isinstance(raw, str | int) and not isinstance(
            raw, bool
        ):
            try:
                number = Decimal(str(raw))
            except InvalidOperation:
                raise _refuse(where, f"a {kind} written as a string or integer") from None
            if number.is_finite():
                return number
        case FieldType.INTEGER if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        case FieldType.DATE if isinstance(raw, str):
            try:
                return date.fromisoformat(raw)
            except ValueError:
                raise _refuse(where, "a date as YYYY-MM-DD") from None
        case FieldType.BOOLEAN if isinstance(raw, bool):
            return raw
        case FieldType.TEXT_LIST if isinstance(raw, str) and raw:
            # contains / not_contains: one member of the list.
            return raw
        case _:
            pass
    raise _refuse(where, f"a {kind} value")


def _leaf(raw: Mapping[str, Any], where: str) -> Leaf:
    name = raw.get("field")
    if name not in FIELDS:
        raise _refuse(f"{where}.field", f"one of {', '.join(sorted(FIELDS))}")
    kind = FIELDS[str(name)]
    op = _enum(Op, raw.get("op"), f"{where}.op")
    if kind not in _OPS[op]:
        raise _refuse(f"{where}.op", f"{op} does not apply to a {kind} field")

    if op in (Op.EXISTS, Op.NOT_EXISTS):
        if "value" in raw:
            raise _refuse(f"{where}.value", f"{op} takes no value")
        return Leaf(str(name), op)
    if "value" not in raw:
        raise _refuse(f"{where}.value", f"{op} needs a value")
    value = raw["value"]
    if op in (Op.IN, Op.NOT_IN):
        if not isinstance(value, list) or not value:
            raise _refuse(f"{where}.value", f"{op} needs a non-empty list")
        return Leaf(str(name), op, tuple(_scalar(kind, v, f"{where}.value") for v in value))
    return Leaf(str(name), op, _scalar(kind, value, f"{where}.value"))


def _condition(raw: object, where: str) -> Condition:
    if not isinstance(raw, Mapping):
        raise _refuse(where, "a condition object")
    joins = [k for k in ("all", "any", "not") if k in raw]
    if len(joins) > 1 or (joins and len(raw) != 1):
        raise _refuse(where, "exactly one of all, any, not, or a field test")
    if not joins:
        return _leaf(raw, where)
    join = joins[0]
    if join == "not":
        return Not(_condition(raw["not"], f"{where}.not"))
    items = raw[join]
    if not isinstance(items, list) or not items:
        raise _refuse(f"{where}.{join}", "a non-empty list of conditions")
    parsed = tuple(_condition(item, f"{where}.{join}[{i}]") for i, item in enumerate(items))
    return AllOf(parsed) if join == "all" else AnyOf(parsed)


def _action(raw: object) -> Action:
    if not isinstance(raw, Mapping):
        raise _refuse("action", "an action object")
    if raw.get("type") != "FLAG":
        raise _refuse("action.type", "FLAG")
    if raw.get("auto_correct", False) is not False:
        # ADR-0007: refused until M4.7 builds the deterministic fixes and
        # their PD-16 refusals.
        raise _refuse("action.auto_correct", "auto-correction is not available yet")
    message = raw.get("message")
    if not isinstance(message, str) or not message.strip():
        raise _refuse("action.message", "a message for the person who works the finding")
    if "{" in message or "}" in message:
        # Fixed text only: nothing from the claim is spliced in (PD-13).
        raise _refuse("action.message", "fixed text, with no placeholders")
    route = raw.get("route_to")
    if route not in ROUTES:
        raise _refuse("action.route_to", f"one of {', '.join(sorted(ROUTES))}")
    return Action(message=message, route_to=str(route))


def parse_rule(document: Mapping[str, Any]) -> Rule:
    """The rule a document describes, or a refusal naming what is wrong."""
    rule_id = document.get("rule_id")
    if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
        raise _refuse("rule_id", "upper-case letters, digits and underscores, 1 to 120")
    layer = _enum(Layer, document.get("layer"), "layer")
    if layer is Layer.LEARNED:
        # Layer 6 is the model's (M6); a hand-written rule there would be
        # read as model output.
        raise _refuse("layer", "LEARNED holds model output, not hand-written rules")
    severity = _enum(Severity, document.get("severity"), "severity")

    scope_raw = document.get("scope") or {}
    if not isinstance(scope_raw, Mapping) or set(scope_raw) - _SCOPE_KEYS:
        raise _refuse("scope", f"only {', '.join(sorted(_SCOPE_KEYS))}")
    scope = Scope(
        payer_ids=_codes(scope_raw.get("payer_id"), "scope.payer_id"),
        cpts=_codes(scope_raw.get("cpt"), "scope.cpt"),
    )

    provenance = document.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        raise _refuse("provenance", "an object")

    return Rule(
        rule_id=rule_id,
        layer=layer,
        severity=severity,
        scope=scope,
        condition=_condition(document.get("condition"), "condition"),
        action=_action(document.get("action")),
        provenance=dict(provenance),
    )
