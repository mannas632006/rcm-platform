"""The rows a model trains on, read from the warehouse (M6.1, ADR-0026).

One row per service line, from `mart_final_outcome`: the lines whose outcome
has stopped moving (DR-09). The mart's grain is one row per line per
adjustment (PD-6), and every adjustment row of a line carries the same
features and the same `is_denied`, so a line is taken once. That is a read of
the mart, not a change to its grain.

A line the payer never adjudicated has `is_denied` null and is left out.
Reading it as "not denied" would teach a model that a rejected claim is a
clean one (PD-4).

`assert_trainable` runs before the query is built (FR-ML-01): a column that is
not a feature never reaches a model because it never reaches the query.

Column names come from `MART_COLUMNS`, an allowlist, and are bound as
SQLAlchemy column objects. No SQL is assembled from strings (S-11).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from rcm.analytics.features import Stage, assert_trainable

#: Where dbt builds the marts (`analytics` + `_marts`; see dbt_project.yml).
MART_SCHEMA: Final = "analytics_marts"
TRAINING_MART: Final = "mart_final_outcome"


class Kind(StrEnum):
    """How a feature reaches a model."""

    #: A code: a payer id, a CPT code, a place of service.
    CATEGORY = "CATEGORY"
    #: A quantity.
    NUMBER = "NUMBER"
    #: A yes or no, read as 1 or 0.
    FLAG = "FLAG"


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    kind: Kind


#: ML-1's inputs (SRS 9.1: "line features"). Every one is a FEATURE in
#: `MART_COLUMNS` with a source today. `submission_date` itself is left out:
#: a raw date only extrapolates; its day of week and month are in.
DENIAL_RISK_FEATURES: Final[tuple[Feature, ...]] = (
    Feature("payer_id", Kind.CATEGORY),
    Feature("plan_type", Kind.CATEGORY),
    Feature("payer_sequence", Kind.CATEGORY),
    Feature("billing_npi", Kind.CATEGORY),
    Feature("place_of_service", Kind.CATEGORY),
    Feature("cpt_hcpcs", Kind.CATEGORY),
    Feature("modifier_1", Kind.CATEGORY),
    Feature("modifier_2", Kind.CATEGORY),
    Feature("modifier_3", Kind.CATEGORY),
    Feature("modifier_4", Kind.CATEGORY),
    Feature("primary_dx", Kind.CATEGORY),
    Feature("dx_2", Kind.CATEGORY),
    Feature("dx_3", Kind.CATEGORY),
    Feature("dx_4", Kind.CATEGORY),
    Feature("is_secondary", Kind.FLAG),
    Feature("auth_number_present", Kind.FLAG),
    Feature("referral_present", Kind.FLAG),
    Feature("units", Kind.NUMBER),
    Feature("dx_pointer", Kind.NUMBER),
    Feature("line_charge", Kind.NUMBER),
    Feature("claim_total_charge", Kind.NUMBER),
    Feature("lines_on_claim", Kind.NUMBER),
    Feature("days_dos_to_submission", Kind.NUMBER),
    Feature("days_to_filing_deadline", Kind.NUMBER),
    Feature("submission_attempt", Kind.NUMBER),
    Feature("submission_dow", Kind.NUMBER),
    Feature("submission_month", Kind.NUMBER),
    Feature("payer_cpt_denial_rate_90d", Kind.NUMBER),
    Feature("payer_cpt_window_size", Kind.NUMBER),
    Feature("payer_provider_denial_rate_90d", Kind.NUMBER),
    Feature("payer_provider_window_size", Kind.NUMBER),
    Feature("cpt_modifier_denial_rate_90d", Kind.NUMBER),
    Feature("cpt_modifier_window_size", Kind.NUMBER),
)

#: A feature value as a model reads it: a code, a number, or missing.
Value = str | float | None


@dataclass(frozen=True, slots=True)
class LineRow:
    """One adjudicated service line: who it is, its features, its answer."""

    service_line_id: uuid.UUID
    claim_id: uuid.UUID
    client_id: uuid.UUID
    payer_id: str | None
    submission_date: date
    values: tuple[Value, ...]
    label: bool


@dataclass(frozen=True, slots=True)
class LineDataset:
    features: tuple[Feature, ...]
    rows: tuple[LineRow, ...]

    def window(self, start: date, end: date) -> LineDataset:
        """The rows submitted in `[start, end)`."""
        return LineDataset(
            self.features, tuple(r for r in self.rows if start <= r.submission_date < end)
        )

    def data_hash(self) -> str:
        """SHA-256 over every row's id, features and label, in id order.

        FR-ML-03 records it with each model: the same hash means the same
        rows, whichever order the database returned them in.
        """
        digest = hashlib.sha256()
        digest.update(json.dumps([f.name for f in self.features]).encode())
        for row in sorted(self.rows, key=lambda r: r.service_line_id):
            line = [str(row.service_line_id), list(row.values), row.label]
            digest.update(json.dumps(line, separators=(",", ":")).encode())
            digest.update(b"\n")
        return digest.hexdigest()


def _value(raw: object, kind: Kind) -> Value:
    if raw is None:
        return None
    if kind is Kind.CATEGORY:
        return str(raw)
    if kind is Kind.FLAG:
        return 1.0 if raw else 0.0
    if isinstance(raw, bool) or not isinstance(raw, int | float | Decimal):
        raise TypeError(f"expected a number, got {type(raw).__name__}")
    return float(raw)


def _mart(columns: Sequence[str], *, name: str = TRAINING_MART) -> sa.TableClause:
    return sa.table(name, *(sa.column(c) for c in columns), schema=MART_SCHEMA)


def _read(
    session: Session,
    features: Sequence[Feature],
    *,
    where: Sequence[sa.ColumnElement[bool]],
    label: sa.ColumnElement[bool],
    first: Sequence[sa.ColumnElement[Any]],
    mart: sa.TableClause,
) -> LineDataset:
    """One row per service line: the first of its rows in `first`'s order."""
    keys = ("service_line_id", "claim_id", "client_id", "submission_date", "payer_id")
    names = list(dict.fromkeys([*keys, *(f.name for f in features)]))
    query = (
        sa.select(*(mart.c[n] for n in names), label.label("label"))
        .where(mart.c.submission_date.is_not(None), *where)
        .order_by(mart.c.service_line_id, *first)
    )
    rows: list[LineRow] = []
    seen: set[uuid.UUID] = set()
    for row in session.execute(query):
        # A line's other adjustment rows repeat its features: keep the first.
        if row.service_line_id in seen:
            continue
        seen.add(row.service_line_id)
        rows.append(
            LineRow(
                service_line_id=row.service_line_id,
                claim_id=row.claim_id,
                client_id=row.client_id,
                payer_id=row.payer_id,
                submission_date=row.submission_date,
                values=tuple(_value(getattr(row, f.name), f.kind) for f in features),
                label=bool(row.label),
            )
        )
    return LineDataset(tuple(features), tuple(sorted(rows, key=lambda r: r.submission_date)))


def denial_lines(
    session: Session, *, features: Sequence[Feature] = DENIAL_RISK_FEATURES
) -> LineDataset:
    """ML-1: every matured, adjudicated service line; the label is `is_denied`."""
    assert_trainable(f.name for f in features)
    names = [f.name for f in features]
    mart = _mart(
        list(
            dict.fromkeys(
                [
                    "service_line_id",
                    "claim_id",
                    "client_id",
                    "submission_date",
                    "payer_id",
                    *names,
                    "is_denied",
                    "adjustment_sequence",
                ]
            )
        )
    )
    return _read(
        session,
        features,
        where=[mart.c.is_denied.is_not(None)],
        label=mart.c.is_denied,
        first=[mart.c.adjustment_sequence.asc().nulls_first()],
        mart=mart,
    )


#: ML-3's inputs (SRS 9.1: "denial + history"): the line as it was sent, and
#: the 835's answer to it (Stage.DENIAL). Nothing after the 835.
RECOVERY_FEATURES: Final[tuple[Feature, ...]] = (
    *DENIAL_RISK_FEATURES,
    Feature("carc_code", Kind.CATEGORY),
    Feature("cas_group_code", Kind.CATEGORY),
    Feature("denial_category", Kind.CATEGORY),
    Feature("preventability", Kind.CATEGORY),
    Feature("adjustment_amount", Kind.NUMBER),
    Feature("days_submission_to_adjudication", Kind.NUMBER),
)


def recovery_lines(
    session: Session, *, features: Sequence[Feature] = RECOVERY_FEATURES
) -> LineDataset:
    """ML-3: every matured denied line; the label is whether it was recovered.

    Recovered means a later submission in the same chain (PD-7: resubmissions
    chain to the original) was paid on the same procedure. Taken at the
    denial: the line's features are as it was sent, its CARC is the payer's
    refusal (a patient-share row is read only when the line has nothing
    else), and the label is the one fact from after it.
    """
    assert_trainable((f.name for f in features), stage=Stage.DENIAL)
    names = [f.name for f in features]
    columns = list(
        dict.fromkeys(
            [
                "service_line_id",
                "claim_id",
                "client_id",
                "submission_date",
                "payer_id",
                *names,
                "is_denied",
                "adjustment_sequence",
                "root_claim_id",
                "submission_attempt",
                "cpt_hcpcs",
                "line_paid",
            ]
        )
    )
    denied = _mart(columns)
    later = _mart(columns).alias("later")
    chain = sa.func.coalesce(denied.c.root_claim_id, denied.c.claim_id)
    recovered = sa.exists().where(
        sa.func.coalesce(later.c.root_claim_id, later.c.claim_id) == chain,
        later.c.client_id == denied.c.client_id,
        later.c.submission_attempt > denied.c.submission_attempt,
        later.c.cpt_hcpcs == denied.c.cpt_hcpcs,
        later.c.line_paid > 0,
    )
    return _read(
        session,
        features,
        where=[denied.c.is_denied.is_(True)],
        label=recovered,
        first=[
            (denied.c.cas_group_code == "PR").asc().nulls_last(),
            denied.c.adjustment_sequence.asc().nulls_first(),
        ],
        mart=denied,
    )
