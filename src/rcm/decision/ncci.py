"""NCCI procedure-to-procedure and MUE edits (M4.3, ADR-0008).

CMS publishes both tables quarterly. A file is parsed whole: one malformed
row refuses it, by row number, and nothing is partly loaded.

The check runs in Layer 3 (CLINICAL). It flags; it never fixes. A pair whose
edit allows a bypass is put to a person, because 25, 59 and X{EPSU} are not
applied automatically (PD-16).
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from rcm.decision.rules.definition import Layer
from rcm.decision.rules.evaluate import ClaimFacts, Finding, LineFacts, Mode, Outcome
from rcm.platform.errors import ValidationFailedError

#: PTP modifier indicators: 0 no bypass, 1 bypass with an NCCI-associated
#: modifier, 9 the edit does not apply.
_PTP_INDICATORS: Final = frozenset({"0", "1", "9"})
#: MUE adjudication indicators: 1 per line, 2 per date (policy), 3 per date
#: (clinical).
_MUE_INDICATORS: Final = frozenset({"1", "2", "3"})

#: CMS's NCCI-associated modifiers: anatomic, global-surgery and the
#: distinct-service family. Any of these on the column-2 line bypasses an
#: indicator-1 edit.
NCCI_ASSOCIATED_MODIFIERS: Final = frozenset(
    {"E1", "E2", "E3", "E4", "FA", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9"}
    | {"LC", "LD", "LM", "LT", "RC", "RI", "RT"}
    | {"TA", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"}
    | {"24", "25", "27", "57", "58", "59", "78", "79", "91"}
    | {"XE", "XP", "XS", "XU"}
)

_CODE: Final = re.compile(r"[0-9A-Z]{5}")
_DATE: Final = re.compile(r"\d{8}")
_PTP_COLUMNS: Final = (
    "column_1",
    "column_2",
    "effective_date",
    "deletion_date",
    "modifier_indicator",
)
_MUE_COLUMNS: Final = ("code", "mue_value", "adjudication_indicator")
_OPTIONAL: Final = frozenset({"rationale"})

ROUTE: Final = "SCRUB_EXCEPTION"


@dataclass(frozen=True, slots=True)
class PtpEdit:
    column_1: str
    column_2: str
    effective: date
    #: None: still in effect. The edit stops applying on this date.
    deleted: date | None
    modifier_indicator: str

    def in_effect(self, on: date) -> bool:
        return self.effective <= on and (self.deleted is None or on < self.deleted)


@dataclass(frozen=True, slots=True)
class MueLimit:
    code: str
    units: Decimal
    adjudication_indicator: str


def _refuse(row: int, reason: str) -> ValidationFailedError:
    return ValidationFailedError(field=f"row {row}", reason=reason)


def _rows(payload: bytes, columns: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationFailedError(field="file", reason="UTF-8 text") from None
    reader = csv.DictReader(io.StringIO(text))
    header = set(reader.fieldnames or ())
    if not set(columns) <= header or header - set(columns) - _OPTIONAL:
        raise ValidationFailedError(field="header", reason=f"columns {', '.join(columns)}")
    # Row 1 is the header, so the first edit is row 2.
    rows = [(number, row) for number, row in enumerate(reader, start=2)]
    if not rows:
        raise ValidationFailedError(field="file", reason="at least one edit")
    for number, row in rows:
        if None in row or any(row.get(c) is None for c in columns):
            raise _refuse(number, "every column, and no extra values")
    return rows


def _code(raw: str, row: int, column: str) -> str:
    code = raw.strip()
    if not _CODE.fullmatch(code):
        raise _refuse(row, f"{column}: a five-character procedure code")
    return code


def _date(raw: str, row: int, column: str) -> date:
    value = raw.strip()
    if not _DATE.fullmatch(value):
        raise _refuse(row, f"{column}: a date as YYYYMMDD")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise _refuse(row, f"{column}: a real date") from None


def parse_ptp(payload: bytes) -> list[PtpEdit]:
    edits: list[PtpEdit] = []
    for number, row in _rows(payload, _PTP_COLUMNS):
        indicator = row["modifier_indicator"].strip()
        if indicator not in _PTP_INDICATORS:
            raise _refuse(number, "modifier_indicator: 0, 1 or 9")
        effective = _date(row["effective_date"], number, "effective_date")
        raw_deleted = row["deletion_date"].strip()
        deleted = None if raw_deleted in ("", "*") else _date(raw_deleted, number, "deletion_date")
        if deleted is not None and deleted <= effective:
            raise _refuse(number, "deletion_date: after effective_date")
        edits.append(
            PtpEdit(
                column_1=_code(row["column_1"], number, "column_1"),
                column_2=_code(row["column_2"], number, "column_2"),
                effective=effective,
                deleted=deleted,
                modifier_indicator=indicator,
            )
        )
    return edits


def parse_mue(payload: bytes) -> list[MueLimit]:
    limits: list[MueLimit] = []
    for number, row in _rows(payload, _MUE_COLUMNS):
        indicator = row["adjudication_indicator"].strip()
        if indicator not in _MUE_INDICATORS:
            raise _refuse(number, "adjudication_indicator: 1, 2 or 3")
        try:
            units = Decimal(row["mue_value"].strip())
        except InvalidOperation:
            raise _refuse(number, "mue_value: a number") from None
        if not units.is_finite() or units <= 0:
            raise _refuse(number, "mue_value: more than zero")
        limits.append(MueLimit(_code(row["code"], number, "code"), units, indicator))
    return limits


@dataclass(frozen=True, slots=True)
class _Line:
    number: int
    code: str
    modifiers: frozenset[str]
    units: Decimal
    #: The line's date, or the claim's when the line has none.
    on: date | None


def _date_or_none(value: object) -> date | None:
    if value is None or isinstance(value, date):
        return value
    raise ValidationFailedError(field="facts", reason="a date of service is a date")


def _lines(facts: ClaimFacts) -> list[_Line]:
    fallback = _date_or_none(facts.claim["claim.date_of_service"])

    def one(line: LineFacts) -> _Line:
        values = line.values
        modifiers = values["line.modifiers"]
        units = values["line.units"]
        if not isinstance(modifiers, list | tuple) or not isinstance(units, Decimal):
            raise ValidationFailedError(field="facts", reason="modifiers a list, units a number")
        return _Line(
            number=line.number,
            code=str(values["line.procedure_code"]),
            modifiers=frozenset(str(m) for m in modifiers),
            units=units,
            on=_date_or_none(values["line.service_date"]) or fallback,
        )

    return [one(line) for line in facts.lines]


@dataclass(frozen=True, slots=True)
class NcciCheck:
    """The PTP and MUE releases a ruleset names, as a Layer 3 check."""

    ptp_release: str | None
    mue_release: str | None
    ptp: Sequence[PtpEdit] = ()
    mue: Sequence[MueLimit] = ()
    layer: Layer = field(default=Layer.CLINICAL)

    def _finding(
        self, rule_id: str, line: int, outcome: Outcome, message: str, reference: str
    ) -> Finding:
        return Finding(
            rule_id=rule_id,
            rule_version=0,
            layer=Layer.CLINICAL,
            mode=Mode.BLOCKING,
            outcome=outcome,
            line_number=line,
            message=message,
            route_to=ROUTE,
            reference=reference,
        )

    def findings(self, facts: ClaimFacts) -> list[Finding]:
        lines = _lines(facts)
        return self._pairs(lines) + self._units(lines)

    def _pairs(self, lines: list[_Line]) -> list[Finding]:
        edits: dict[tuple[str, str], list[PtpEdit]] = defaultdict(list)
        for edit in self.ptp:
            if edit.modifier_indicator != "9":
                edits[(edit.column_1, edit.column_2)].append(edit)

        found: list[Finding] = []
        for second in lines:
            for column_1 in sorted({line.code for line in lines if line is not second}):
                candidates = edits.get((column_1, second.code))
                if not candidates:
                    continue
                firsts = [line for line in lines if line is not second and line.code == column_1]
                finding = self._pair(second, firsts, candidates)
                if finding is not None:
                    found.append(finding)
        return found

    def _pair(
        self, second: _Line, firsts: list[_Line], candidates: list[PtpEdit]
    ) -> Finding | None:
        def ref(edit: PtpEdit) -> str:
            return (
                f"ncci-ptp {self.ptp_release} {edit.column_1}/{edit.column_2} "
                f"mi={edit.modifier_indicator}"
            )

        if second.on is None or any(first.on is None for first in firsts):
            # Without a date the edit's effective dates and the same-day test
            # cannot be read: not a pass (ADR-0007 Decision 5).
            return self._finding(
                "NCCI_PTP",
                second.number,
                Outcome.INDETERMINATE,
                "NCCI pair on this claim, but the date of service is missing",
                ref(candidates[0]),
            )
        if not any(first.on == second.on for first in firsts):
            return None
        live = [edit for edit in candidates if edit.in_effect(second.on)]
        if not live:
            return None
        edit = live[0]
        if edit.modifier_indicator == "0":
            return self._finding(
                "NCCI_PTP",
                second.number,
                Outcome.FLAGGED,
                "NCCI bundles this code into another on the claim; no modifier bypasses it",
                ref(edit),
            )
        if second.modifiers & NCCI_ASSOCIATED_MODIFIERS:
            return None  # EC-37: a valid bypass
        return self._finding(
            "NCCI_PTP",
            second.number,
            Outcome.FLAGGED,
            "NCCI bundles this code into another on the claim; a person decides "
            "whether a distinct-service modifier applies",
            ref(edit),
        )

    def _units(self, lines: list[_Line]) -> list[Finding]:
        limits = {limit.code: limit for limit in self.mue}
        found: list[Finding] = []

        def over(limit: MueLimit, line: int, outcome: Outcome) -> None:
            found.append(
                self._finding(
                    "NCCI_MUE",
                    line,
                    outcome,
                    "Units billed exceed the medically unlikely edit for this code",
                    f"ncci-mue {self.mue_release} {limit.code} "
                    f"max={limit.units} mai={limit.adjudication_indicator}",
                )
            )

        for code in sorted({line.code for line in lines} & set(limits)):
            limit = limits[code]
            of_code = [line for line in lines if line.code == code]
            if limit.adjudication_indicator == "1":
                for line in of_code:
                    if line.units > limit.units:
                        over(limit, line.number, Outcome.FLAGGED)
                continue
            by_day: dict[date | None, list[_Line]] = defaultdict(list)
            for line in of_code:
                by_day[line.on].append(line)
            for on, day in sorted(
                by_day.items(), key=lambda kv: (kv[0] is None, kv[0] or date.min)
            ):
                if sum(line.units for line in day) > limit.units:
                    # Undated lines might fall on several days, so over the
                    # limit is unknown for them, not certain.
                    outcome = Outcome.INDETERMINATE if on is None else Outcome.FLAGGED
                    over(limit, day[0].number, outcome)
        return found
