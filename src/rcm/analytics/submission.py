"""A claim's model inputs the second before it is sent (ADR-0032).

    PD-11: Features must be knowable at submission time. No leakage.
    DR-07 (MUST): All trailing-window features MUST be computed with an
    as-of date equal to the claim's submission timestamp.

ML-1 decides before a claim is sent. `mart_claim_line_outcome` is built in
batch, so a claim being scrubbed now is not in it yet, and serving from the
mart alone left the live scrub with no score (ADR-0028). This module computes
the same features from CORE, for the claims asked about.

**The same expressions as the warehouse.** Each claim and line feature is
written as `stg_claims`, `core_claim_submission` and `core_line_submission`
write it. Each trailing rate is written as `core_trailing_denial_rates`
writes it: adjudications that became known in the ninety days up to the day
before the submission date, counted by payment date, across every client.
Two descriptions of one feature drift apart unless something holds them
together, so `tests/leakage/test_ml_serving.py` does: for every line the
warehouse describes, this module gives exactly the mart's values.

**The history is the mart's.** A trailing rate counts past adjudications,
which is where the mart keeps them. The claim being scored needs no row
there, and its own outcome is never read: the window closes the day before
it is sent.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased

from rcm.analytics.dataset import (
    DENIAL_RISK_FEATURES,
    MART_SCHEMA,
    SERVING_MART,
    Feature,
    Value,
    as_value,
)
from rcm.analytics.features import Stage, assert_trainable
from rcm.domain.claim import Claim, ClaimLine
from rcm.domain.payer import Payer
from rcm.domain.remittance import RawFile
from rcm.platform.errors import ValidationFailedError

#: `core_trailing_denial_rates`: the window is `[as_of - 90 days, as_of - 1
#: day]`. How a feature is defined, not a limit on it (FR-ML-07).
WINDOW_DAYS: Final = 90
_RATE_PLACES: Final = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class SubmissionRow:
    """One submitted service line and a model's inputs for it. It has no
    `service_line_id`: that comes from an 835 nobody has received yet."""

    claim_line_id: uuid.UUID
    claim_id: uuid.UUID
    client_id: uuid.UUID
    payer_id: str
    submission_date: date
    values: tuple[Value, ...]


@dataclass(frozen=True, slots=True)
class _Window:
    denied: int
    seen: int

    @property
    def rate(self) -> Decimal | None:
        # Null, not zero, when the window held nothing: "not known" is not
        # "never denied" (core_trailing_denial_rates).
        if not self.seen:
            return None
        return (Decimal(self.denied) / Decimal(self.seen)).quantize(
            _RATE_PLACES, rounding=ROUND_HALF_UP
        )


def _history() -> sa.TableClause:
    return sa.table(
        SERVING_MART,
        *(
            sa.column(name)
            for name in (
                "service_line_id",
                "payer_id",
                "billing_npi",
                "cpt_hcpcs",
                "modifier_1",
                "submission_date",
                "days_submission_to_adjudication",
                "is_denied",
            )
        ),
        schema=MART_SCHEMA,
    )


def _trailing(
    session: Session,
    *,
    payer_id: str,
    billing_npi: str,
    cpt: str,
    modifier_1: str | None,
    as_of: date,
) -> tuple[_Window, _Window, _Window]:
    """(payer x CPT, payer x provider, CPT x modifier) as of `as_of`."""
    mart = _history()
    # The day the payer's decision arrived: the day anyone could have known it.
    known_on = mart.c.submission_date + mart.c.days_submission_to_adjudication
    # The mart's grain is one row per adjustment; a line is counted once.
    lines = (
        sa.select(
            mart.c.service_line_id,
            mart.c.payer_id,
            mart.c.billing_npi,
            mart.c.cpt_hcpcs,
            mart.c.modifier_1,
            mart.c.is_denied,
        )
        .where(
            mart.c.service_line_id.is_not(None),
            mart.c.days_submission_to_adjudication.is_not(None),
            known_on >= as_of - timedelta(days=WINDOW_DAYS),
            # The day itself is left out: at date granularity "earlier today"
            # cannot be told from "later today" (PD-18).
            known_on <= as_of - timedelta(days=1),
            sa.or_(mart.c.payer_id == payer_id, mart.c.cpt_hcpcs == cpt),
        )
        .distinct()
        .subquery()
    )
    payer_cpt = sa.and_(lines.c.payer_id == payer_id, lines.c.cpt_hcpcs == cpt)
    payer_provider = sa.and_(lines.c.payer_id == payer_id, lines.c.billing_npi == billing_npi)
    # A window partition puts lines with no modifier together; so does this.
    cpt_modifier = sa.and_(
        lines.c.cpt_hcpcs == cpt, lines.c.modifier_1.is_not_distinct_from(modifier_1)
    )
    denied = lines.c.is_denied.is_(True)
    row = session.execute(
        sa.select(
            sa.func.count().filter(payer_cpt, denied),
            sa.func.count().filter(payer_cpt),
            sa.func.count().filter(payer_provider, denied),
            sa.func.count().filter(payer_provider),
            sa.func.count().filter(cpt_modifier, denied),
            sa.func.count().filter(cpt_modifier),
        ).select_from(lines)
    ).one()
    return (
        _Window(int(row[0]), int(row[1])),
        _Window(int(row[2]), int(row[3])),
        _Window(int(row[4]), int(row[5])),
    )


def submission_lines(
    session: Session,
    *,
    client_id: uuid.UUID,
    claim_ids: Sequence[uuid.UUID],
    features: Sequence[Feature] = DENIAL_RISK_FEATURES,
) -> list[SubmissionRow]:
    """One row per submitted line of these claims, with a model's inputs.

    The leakage gate runs here as it does for training and for serving from
    the mart (FR-ML-01). Only this client's claims are read (PD-20).
    """
    assert_trainable((f.name for f in features), stage=Stage.SUBMISSION)
    # stg_claims: the date the submitter stated, else the day the file was
    # archived. The as-of date of every trailing window.
    submission_date = sa.func.coalesce(
        Claim.submitted_on, sa.cast(RawFile.received_at.op("AT TIME ZONE")("UTC"), sa.Date)
    )
    # Its own name for the table: the outer query reads claim_line too.
    sibling = aliased(ClaimLine)
    lines_on_claim = (
        sa.select(sa.func.count())
        .select_from(sibling)
        .where(sibling.claim_id == Claim.claim_id)
        .correlate(Claim)
        .scalar_subquery()
    )
    query = (
        sa.select(
            ClaimLine.claim_line_id,
            Claim.claim_id,
            Claim.client_id,
            Claim.payer_id,
            sa.func.nullif(Payer.plan_type, "").label("plan_type"),
            Claim.payer_responsibility.label("payer_sequence"),
            (Claim.payer_responsibility != "P").label("is_secondary"),
            Claim.billing_npi,
            sa.func.nullif(Claim.place_of_service, "").label("place_of_service"),
            ClaimLine.procedure_code.label("cpt_hcpcs"),
            ClaimLine.modifier_1,
            ClaimLine.modifier_2,
            ClaimLine.modifier_3,
            ClaimLine.modifier_4,
            Claim.diagnoses[1].label("primary_dx"),
            Claim.diagnoses[2].label("dx_2"),
            Claim.diagnoses[3].label("dx_3"),
            Claim.diagnoses[4].label("dx_4"),
            sa.func.nullif(Claim.prior_auth_number, "").is_not(None).label("auth_number_present"),
            sa.func.nullif(Claim.referral_number, "").is_not(None).label("referral_present"),
            ClaimLine.units,
            ClaimLine.diagnosis_pointers[1].label("dx_pointer"),
            ClaimLine.charge_amount.label("line_charge"),
            Claim.total_charge.label("claim_total_charge"),
            lines_on_claim.label("lines_on_claim"),
            Claim.submission_attempt,
            Claim.date_of_service,
            Payer.timely_filing_days,
            submission_date.label("submission_date"),
        )
        .select_from(ClaimLine)
        .join(Claim, Claim.claim_id == ClaimLine.claim_id)
        .join(RawFile, RawFile.raw_file_id == Claim.raw_file_id)
        .outerjoin(Payer, Payer.payer_id == Claim.payer_id)
        .where(Claim.client_id == client_id, Claim.claim_id.in_(list(dict.fromkeys(claim_ids))))
        .order_by(Claim.claim_id, ClaimLine.line_number)
    )
    windows: dict[tuple[str, str, str, str | None, date], tuple[_Window, _Window, _Window]] = {}
    rows: list[SubmissionRow] = []
    for line in session.execute(query):
        sent: date = line.submission_date
        key = (line.payer_id, line.billing_npi, line.cpt_hcpcs, line.modifier_1, sent)
        if key not in windows:
            windows[key] = _trailing(
                session,
                payer_id=line.payer_id,
                billing_npi=line.billing_npi,
                cpt=line.cpt_hcpcs,
                modifier_1=line.modifier_1,
                as_of=sent,
            )
        payer_cpt, payer_provider, cpt_modifier = windows[key]
        # core_claim_submission: measured from the business submission date.
        # Negative means already late when sent: a real state, not clamped.
        waited = (sent - line.date_of_service).days if line.date_of_service else None
        raw: dict[str, Any] = {
            **line._mapping,
            "days_dos_to_submission": waited,
            "days_to_filing_deadline": (
                line.timely_filing_days - waited
                if waited is not None and line.timely_filing_days is not None
                else None
            ),
            "submission_dow": sent.isoweekday(),
            "submission_month": sent.month,
            "payer_cpt_denial_rate_90d": payer_cpt.rate,
            "payer_cpt_window_size": payer_cpt.seen,
            "payer_provider_denial_rate_90d": payer_provider.rate,
            "payer_provider_window_size": payer_provider.seen,
            "cpt_modifier_denial_rate_90d": cpt_modifier.rate,
            "cpt_modifier_window_size": cpt_modifier.seen,
        }
        unknown = [f.name for f in features if f.name not in raw]
        if unknown:
            # A feature the warehouse has and this module does not would be
            # scored as missing: refused by name instead (PD-18).
            raise ValidationFailedError(
                field="features",
                reason=f"not computed at submission: {', '.join(unknown)}",
            )
        rows.append(
            SubmissionRow(
                claim_line_id=line.claim_line_id,
                claim_id=line.claim_id,
                client_id=line.client_id,
                payer_id=line.payer_id,
                submission_date=sent,
                values=tuple(as_value(raw[f.name], f.kind) for f in features),
            )
        )
    return rows
